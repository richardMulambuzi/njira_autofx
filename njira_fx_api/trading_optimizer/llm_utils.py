import logging
import os

import google.generativeai as genai

logger = logging.getLogger(__name__)

# Set your Gemini API Key — sourced from the environment (.env -> GEMINI_API_KEY).
# NEVER hardcode credentials in source. If unset, warn and skip configuration so
# the app still boots; Gemini calls will then fail with a clear SDK error.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    logger.warning(
        "GEMINI_API_KEY is not set in the environment. "
        "Gemini features in trading_optimizer.llm_utils will fail until it is configured."
    )

# model = genai.GenerativeModel("gemini-pro")
model = genai.GenerativeModel("gemini-1.5-flash")

def get_strategy_refinement(past_trades: str, current_signal: str) -> str:
    prompt = f"""
You are an expert forex trading assistant.

### Past Trades (last 90 days):
{past_trades}

### Current Signal:
{current_signal}

### Task:
Based on the above, provide a structured response with the following keys:

- signal_quality: Integer (1 to 10)
- refinement_suggestions: A list of improvements for the signal
- risk_reward_evaluation:
  - recommended_stop_loss
  - recommended_take_profit
  - suggested_position_size
- market_context:
  - volatility_impact
  - economic_events_to_watch

Respond in valid JSON format only. No extra explanation.
    """

    response = model.generate_content(prompt)
    return response.text.strip()

# method to analyse data and find the best optimal time
def get_optimal_times_analysis(performance_data: str) -> str:
    prompt = f"""
You are a trading performance analyst.

Below is a list of trades with their timestamp, trading symbol, and profit/loss value. Analyze this data to identify:

1. The most profitable days of the week.
2. The most profitable hours of the day.
3. Symbol-specific patterns (e.g., EURUSD performs best on Tuesday at 10 AM).
4. Any times/days to avoid.

Format your response in JSON like:

{{
  "best_days": ["Tuesday", "Wednesday"],
  "best_hours": ["10:00–12:00", "15:00–17:00"],
  "symbol_insights": {{
    "EURUSD": {{
      "best_days": ["Tuesday"],
      "best_hours": ["10:00–12:00"]
    }},
    "XAUUSD": {{
      "best_days": ["Thursday"],
      "best_hours": ["13:00–14:00"]
    }}
  }},
  "notes": "Avoid Monday mornings due to high volatility and low profitability."
}}

### Trades (last 90 days):
{performance_data}
"""
    response = model.generate_content(prompt)
    return response.text.strip()


# method to give us a block where each symbol reaches a maximum pick, and goes up stable or down trend in a day
#  eg friday from 2-4pm, direction uptrend, 
#  format:
""" json
{
day:
timeblock:
direction:
max_price:
symbol:
}

"""
def get_stable_block_analysis(candle_data: str, symbol: str) -> str:
    prompt = f"""
You are a financial market analyst AI.

Your task is to analyze 15-minute candle data for the symbol {symbol} and identify **stable trading blocks** — time ranges during a day refer to timestamp column for specific day where the price shows a consistent direction (either stable uptrend or downtrend), reaching a peak (max price) during that block.

For each block you find, return a JSON object like this:

{{
  "day": "Friday",
  "timeblock": "14:00–16:00",
  "direction": "uptrend",
  "max_price": 1.2453,
  "symbol": "{symbol}"
}}

You can return multiple blocks in a list.

### Here is the candle data (format: timestamp | O: open | H: high | L: low | C: close):

{candle_data}
"""

    response = model.generate_content(prompt)
    return response.text.strip()
