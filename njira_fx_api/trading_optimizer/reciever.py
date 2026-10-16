from django.shortcuts import render
from typing import List, Dict, Any
from django.http import JsonResponse
from .llm_utils import get_strategy_refinement
from forex.clickhouse.connection import get_clickhouse_client
import json
import re
from .llm_utils import get_optimal_times_analysis
from .llm_utils import get_stable_block_analysis
from datetime import datetime
# ponytail: legacy constants import removed (analysis_module.constants deleted;
# values moved to .env). Only ANSI colors are used below — defined inline.
import os as _os
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_BLUE = "\033[94m"
_RED = "\033[91m"
_RESET = "\033[0m"
_MAX_RETRIES = int(_os.environ.get("MAX_RETRIES", "3"))

# from datetime import datetime
# import json
# import re
# from db.clickhouse import get_clickhouse_client
# from ai_models.stable_block import get_stable_block_analysis  # Ensure this returns a string (AI response)
# def refine_signals(signals: List[Dict[str, Any]]):
def refine_signals(signals: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    client = get_clickhouse_client()

    # Fetch past trades
    past_trades_query = """
        SELECT timestamp, contract_type, profit_loss 
        FROM trades 
        WHERE timestamp >= now() - INTERVAL 90 DAY
        ORDER BY timestamp DESC
        LIMIT 100
    """
    s_result = client.query(past_trades_query)
    trades_result = s_result.result_set
    past_trades = "\n".join([
        f"{row[0]}: {row[1]}, result: {row[2]} USD" for row in trades_result
    ])

    # Fetch past signals
    past_signals_query = """
        SELECT timestamp, Signal, Entry, SL, TP, Pair, Strategy 
        FROM trading_signals 
        WHERE timestamp >= now() - INTERVAL 90 DAY
        ORDER BY timestamp DESC
        LIMIT 100
    """
    result = client.query(past_signals_query)
    signals_result = result.result_set
    past_signals = "\n".join([
        f"{row[0]}: {row[1]} @ {row[2]}, SL: {row[3]}, TP: {row[4]}, Pair: {row[5]}, Strategy: {row[6]}" for row in signals_result
    ])

    refined_outputs = []
    for signal in signals:
        current_signal = f"{signal['Signal']} @ {signal['Entry']}, SL: {signal['SL']}, TP: {signal['TP']}, Pair: {signal['Pair']}, Strategy: {signal['Strategy']}"

        # Run through the model
        result = get_strategy_refinement(past_trades + "\n" + past_signals, current_signal)
        refined_outputs.append({
            "original": current_signal,
            "refined": result
        })

        # print(refined_outputs)

        for item in refined_outputs:
            print("Original Signal:")
            print(item["original"])

            # Step 1: Extract JSON block from Markdown
            json_block = re.sub(r"^```json\n|\n```$", "", item["refined"].strip())

            try:
                # Step 2: Parse it
                refined_data = json.loads(json_block)

                # Step 3: Pretty print
                print("\n🔹 Refined Signal JSON:")
                print(json.dumps(refined_data, indent=2))

                # Step 4: Access specific parameters
                print("\n🔹 Extracted Parameters:")
                print("Signal Quality:", refined_data["signal_quality"])
                print("Suggested SL:", refined_data["risk_reward_evaluation"]["recommended_stop_loss"])
                print("Suggested TP:", refined_data["risk_reward_evaluation"]["recommended_take_profit"])

                # saving suggestions to database
                # Ensure the table exists
                table_name = "ai_analysis"
                create_table_query = f"""
                    CREATE TABLE IF NOT EXISTS {table_name} (
                        timestamp DateTime,
                        entry Float32,
                        tp Float32,
                        sl Float32,
                        new_tp Float32,
                        new_sl Float32,
                        signal_quality Float32,
                        symbol String,
                        signal String
                    ) ENGINE = MergeTree()
                    ORDER BY timestamp
                """
            
                client.command(create_table_query)
                
                # Check if the timestamp already exists
                insert_query = f"""
                    INSERT INTO {table_name} (timestamp, entry, tp, sl, new_tp, new_sl, signal_quality, symbol, signal)
                    VALUES (
                        '{signal['Timestamp']}', 
                        {signal['Entry']}, 
                        {signal['TP']}, 
                        {signal['SL']},  
                        {refined_data["risk_reward_evaluation"]["recommended_take_profit"]}, 
                        {refined_data["risk_reward_evaluation"]["recommended_stop_loss"]}, 
                        {refined_data["signal_quality"]}, 
                        '{signal['Pair']}', 
                        '{signal['Signal']}'
                    )
                """
                client.command(insert_query)

         

            except json.JSONDecodeError as e:
                print("Failed to parse JSON:", e)
    return JsonResponse({"results": refined_outputs})

# method to analyse the perfomance of each symbol based on working days
async def analyze_optimal_trading_windows():
    print(f"{_GREEN} *******************Optimisation process running*******************.{_RESET}")
    client = get_clickhouse_client()

    # Fetch past trades
    query1 = """
        SELECT timestamp, symbol, take_profit, stop_loss, sell_time, buy_price, sell_price, profit_loss 
        FROM trades 
        WHERE timestamp >= now() - INTERVAL 90 DAY
        ORDER BY timestamp DESC
        LIMIT 1000
    """
    result = client.query(query1).result_set

    # Format trades for model
    performance_data = "\n".join([
        f"{row[0]} | {row[1]} | Take_profit: {row[2]} | Stop_Loss: {row[3]} | Sell_time: {row[4]} | Buy_Price: {row[5]} | Sell_price: {row[6]} | Profit/Loss: {row[7]} USD" for row in result
    ])

    try:
        # Get AI output
        # ai_response = get_optimal_times_analysis(performance_data)
      
        ai_response = get_optimal_times_analysis(performance_data)

        # Clean and parse JSON block
        json_block = re.sub(r"^```json\s*|```$", "", ai_response.strip())

        insights = json.loads(json_block)
        # Create table if not exists
        create_table_query = """
        CREATE TABLE IF NOT EXISTS optimal_time (
            timestamp DateTime,
            symbol String,
            best_days Array(String),
            best_hours Array(String),
            notes String
        ) ENGINE = MergeTree()
        ORDER BY (timestamp, symbol)
        """
        client.command(create_table_query)

        # Save each symbol's insight
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        def format_array_for_clickhouse(array):
            # ClickHouse expects ['item1', 'item2'] format — single quotes inside square brackets
            return "[" + ", ".join(f"'{item}'" for item in array) + "]"

        for symbol, info in insights.get("symbol_insights", {}).items():
            try:
                best_days = format_array_for_clickhouse(info.get("best_days", []))
                best_hours = format_array_for_clickhouse(info.get("best_hours", []))
                notes = insights.get("notes", "").replace("'", "''")  # escape single quotes for SQL string

                insert_query = f"""
                INSERT INTO optimal_time (timestamp, symbol, best_days, best_hours, notes)
                VALUES (
                    '{now}',
                    '{symbol}',
                    {best_days},
                    {best_hours},
                    '{notes}'
                )
                """
                # print("INSERT QUERY:", insert_query, flush=True)  # Show full query
                client.command(insert_query)
                print(f" {_GREEN} Inserted for {symbol}{_RESET}", flush=True)

            except Exception as insert_error:
                print(f"{_RED}Error inserting for {symbol}: {insert_error}{_RESET}", flush=True)

        # Print results
        print(f"{_BLUE}\nOptimal Trading Windows{_RESET}")
        print(f"{_BLUE}Best Days Overall: {insights.get('best_days', [])} {_RESET}")
        print(f"{_BLUE}Best Hours Overall: {insights.get('best_hours', [])}{_RESET}")
        print(f"{_YELLOW} Symbol-specific Insights: {_RESET}")

        for symbol, info in insights.get("symbol_insights", {}).items():
            print(f"{_BLUE} {symbol}: Days - {info['best_days']}, Hours - {info['best_hours']}{_RESET}")

        # confirm done
        print(f"{_GREEN} \n*******************Optimisation process Done******************* \n.{_RESET}")
    except Exception as e:
        return JsonResponse({"error": f"Failed to analyze performance: {e}"}, status=500)


# format array
SYMBOL_AND_TABLE = {
    "frxEURUSD": {"15min_candles": "eurousd_m15_candles"},
    "R_75": {"15min_candles": "v75_15m_candles"},
    "frxGBPUSD": {"15min_candles": "gbpusd_15m_candles"},
    "OTC_AS51": {"15min_candles": "australia200_15m_candles"},
    "frxUSDJPY": {"15min_candles": "usdjpy_15m_candles"},
    "OTC_SPC": {"15min_candles": "us500_15m_candles"},
    "frxXAUUSD": {"15min_candles": "gold_15m_candles"},
}

async def analyze_stable_blocks():
    client = get_clickhouse_client()
    print("🟡 Starting stable block analysis...")

    # Create table if not exists
    create_table_query = """
    CREATE TABLE IF NOT EXISTS stable_hours_block (
        timestamp DateTime,
        day String,
        timeblock String,
        direction String,
        max_price Float64,
        symbol String
    ) ENGINE = MergeTree()
    ORDER BY (timestamp, symbol, day, timeblock)
    """
    client.command(create_table_query)

    for symbol, tables in SYMBOL_AND_TABLE.items():
        table_name = tables["15min_candles"]
        print(f"🔍 Fetching 15min candle data for: {symbol}")

        query = f"""
            SELECT timestamp, open, high, low, close
            FROM {table_name}
            WHERE timestamp >= now() - INTERVAL 90 DAY
            ORDER BY timestamp ASC
        """

        try:
            result = client.query(query).result_set
            if not result:
                print(f"⚠️ No data found for {symbol}")
                continue

            # Format candle data
            candle_data = "\n".join([
                f"{row[0]} | O: {row[1]} | H: {row[2]} | L: {row[3]} | C: {row[4]}"
                for row in result
            ])

            # 🔍 AI analysis
            ai_response = get_stable_block_analysis(candle_data, symbol)
            json_block = re.sub(r"^```json\s*|```$", "", ai_response.strip())

            try:
                insights = json.loads(json_block)
            except json.JSONDecodeError as e:
                print(f"❌ JSON decoding error for {symbol}: {e}")
                print("AI raw response:", ai_response)
                continue

            # Insert results
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            for item in insights:
                insert_query = f"""
                INSERT INTO stable_hours_block (timestamp, day, timeblock, direction, max_price, symbol)
                VALUES (
                    '{now}',
                    '{item["day"]}',
                    '{item["timeblock"]}',
                    '{item["direction"]}',
                    {item["max_price"]},
                    '{item["symbol"]}'
                )
                """
                client.command(insert_query)
                print(f"✅ Inserted block for {item['symbol']} {item['day']} {item['timeblock']}")

        except Exception as e:
            print(f"❌ Error analyzing {symbol}: {e}")

    print("✅ Stable block analysis completed.")
