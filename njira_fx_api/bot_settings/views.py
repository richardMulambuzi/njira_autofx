from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json
from forex.clickhouse.connection import get_clickhouse_client
from datetime import datetime, date 
import pytz

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from rest_framework import serializers

from notifications.views import send_email  

# JWT auth guard for async (coroutine) views — DRF @api_view does not support async
from auth_app.decorators import jwt_required_async  

# Define your parameters for documentation
token_param = openapi.Parameter('token', openapi.IN_BODY, description="Authentication token", type=openapi.TYPE_STRING)
email_param = openapi.Parameter('email', openapi.IN_BODY, description="User's email", type=openapi.TYPE_STRING)
strategy_param = openapi.Parameter('strategy', openapi.IN_BODY, description="Trading strategy", type=openapi.TYPE_STRING)
trading_param = openapi.Parameter('trading', openapi.IN_BODY, description="Trading details", type=openapi.TYPE_BOOLEAN)

class EmailSerializer(serializers.Serializer):
    email = serializers.EmailField()

# @swagger_auto_schema(
#     method='POST', 
#     request_body=openapi.Schema(
#         type=openapi.TYPE_OBJECT,
#         properties={
#             'token': openapi.Schema(type=openapi.TYPE_STRING),
#             'email': openapi.Schema(type=openapi.TYPE_STRING),
#             'strategy': openapi.Schema(type=openapi.TYPE_STRING),  # strategy as a string
#             'trading': openapi.Schema(type=openapi.TYPE_STRING)
#         }
#     )
# )
# @api_view(['POST'])
# Saving user token to the database and strategy
@csrf_exempt
@jwt_required_async
async def save_token_and_strategy(request):
    client = get_clickhouse_client()
    # name = request.query_params.get('name', None)
    if request.method == 'POST':
        try:

            client.command("""
                CREATE TABLE IF NOT EXISTS userdetails (
                    email String,
                    -- Legacy Deriv broker token column (Security Gate 1 #19):
                    -- retained for read-compat with the decommissioned
                    -- analysis_module flow (SELECT token FROM userdetails);
                    -- no longer written by this view.
                    token String,
                    strategy String,
                    trading String,
                    trading_today String,
                    balance Float32,
                    balance_today Float32, 
                    created_at DateTime,
                    started_at DateTime
                ) ENGINE = MergeTree()
                ORDER BY (email);
            """)
            # Parse JSON data from the request
            data = json.loads(request.body)

            # NOTE (Security Gate 1 #19): the Deriv broker token is no longer
            # persisted. The request may still carry a 'token' field for
            # backward compatibility with older clients — it is accepted and
            # ignored.
            email = data.get('email')
            strategy = data.get('strategy')
            trading = data.get('trading')

            # Validate input
            if not strategy or not email:
                return JsonResponse({"error": "Email and strategy are required"}, status=400)

            # Check if the user exists in the database (parameterized query)
            result = client.query(
                """
                SELECT COUNT(*)
                FROM userdetails
                WHERE email = %(email)s
                """,
                {"email": email},
            )
            row_data = result.result_set
            count = row_data[0][0] if row_data else 0

            if count > 0:
                # Update the strategy (token is no longer persisted — Deriv
                # broker decommissioned, Security Gate 1 #19).
                client.command(
                    """
                    ALTER TABLE userdetails UPDATE strategy = %(strategy)s
                    WHERE email = %(email)s
                    """,
                    {"strategy": strategy, "email": email},
                )
            else:
                # Legacy broker balance API removed — record 0.0 until the
                # Pepperstone account integration provides a balance source.
                account_balance = 0.0
                # Insert new data if the user doesn't exist (parameterized;
                # token column intentionally omitted — Security Gate 1 #19)
                client.command(
                    """
                    INSERT INTO userdetails (email, strategy, trading, balance, balance_today, created_at)
                    VALUES (%(email)s, %(strategy)s, %(trading)s, %(account_balance)s, %(account_balance)s, NOW())
                    """,
                    {
                        "email": email,
                        "strategy": strategy,
                        "trading": str(trading),
                        "account_balance": account_balance,
                    },
                )

            return JsonResponse({"message": "Data saved successfully"}, status=201)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

# Updating the trading status of the user
@swagger_auto_schema(method='PUT', 
    request_body=openapi.Schema(
        type=openapi.TYPE_OBJECT,
        properties={
            'email': openapi.Schema(type=openapi.TYPE_STRING),
            'trading': openapi.Schema(type=openapi.TYPE_BOOLEAN)
        }
    )
)
@api_view(['PUT'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def update_trading_status(request):
    if request.method == 'PUT':
        try:
            client = get_clickhouse_client()
            # Parse JSON data from the request
            data = json.loads(request.body)
            email = data.get('email')
            trading = data.get('trading')

            # Validate input
            if not email or trading is None:
                return JsonResponse({"error": "Token, email and trading status are required"}, status=400)

            # Update the trading status in ClickHouse (parameterized)
            if trading:
                client.command(
                    """
                    ALTER TABLE userdetails UPDATE trading = %(trading)s, started_at = NOW()
                    WHERE email = %(email)s
                    """,
                    {"trading": str(trading), "email": email},
                )
            else:
                client.command(
                    """
                    ALTER TABLE userdetails UPDATE trading = %(trading)s
                    WHERE email = %(email)s
                    """,
                    {"trading": str(trading), "email": email},
                )

            return JsonResponse({"message": "Trading status updated successfully"}, status=200)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

# Getting the time the user allowed the bot to start trading
@swagger_auto_schema(
    method='GET',
    query_serializer=EmailSerializer,  # Use query_serializer for GET requests
    responses={200: openapi.Response('Success')}
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def get_start_time(request):
    if request.method == 'GET':
        try:
            client = get_clickhouse_client()
            # Access query parameters
            email = request.query_params.get('email')

            if not email:
                return JsonResponse({"error": "email is required"}, status=400)

            # Fetch the start_time from the database (parameterized query)
            result = client.query(
                """
                SELECT started_at, trading
                FROM userdetails 
                WHERE email = %(email)s
                """,
                {"email": email},
            )
            
            if result:
                data = result.result_set[0]
                timestamp = data[0]
                trading = data[1]
                formatted_timestamp = timestamp.isoformat()
                return JsonResponse({"start_time": formatted_timestamp, 'trading':trading}, status=200)
            else:
                return JsonResponse({"error": "Token not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

# Getting the strategy that the user selected
@swagger_auto_schema(
    method='GET',
    query_serializer=EmailSerializer,  # Use query_serializer for GET requests
    responses={200: openapi.Response('Success')}
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def get_strategy(request):
    if request.method == 'GET':
        try:
            client = get_clickhouse_client()
            # Parse JSON data from the request
            email = request.query_params.get('email')

            if not email:
                return JsonResponse({"error": "email is required"}, status=400)

            # Fetch the strategy from the database (parameterized query)
            result = client.query(
                """
                SELECT strategy
                FROM userdetails 
                WHERE email = %(email)s
                """,
                {"email": email},
            )
            
            if result:
                data = result.result_set[0]
                strategy= data[0]
                return JsonResponse({"strategy": strategy}, status=200)
            else:
                return JsonResponse({"error": "Token not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

""" 
user symols settings
saving saving symbols 

deleting symbols and 
getting user symbols

"""
# Saving symbols 
@swagger_auto_schema(method='post', 
    request_body=openapi.Schema(
        type=openapi.TYPE_OBJECT,
        properties={
            'email': openapi.Schema(type=openapi.TYPE_STRING),
            'token': openapi.Schema(type=openapi.TYPE_STRING),
            'symbol': openapi.Schema(type=openapi.TYPE_STRING),
        }
    )
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def save_symbols(request):
    if request.method == 'POST':
        try:
            client = get_clickhouse_client()
            client.command("""
                CREATE TABLE IF NOT EXISTS symbols (
                    email String,
                    -- Legacy Deriv broker token column (Security Gate 1 #19):
                    -- retained for read-compat with the decommissioned
                    -- analysis_module flow (SELECT ... WHERE token = ...);
                    -- no longer written by this view.
                    token String,
                    symbol String,
                    created_at DateTime
                ) ENGINE = MergeTree()
                ORDER BY (email);
            """)
            # Parse JSON data from the request
            data = json.loads(request.body)

            # NOTE (Security Gate 1 #19): the Deriv broker token is no longer
            # persisted. A 'token' field in the request is accepted and
            # ignored for backward compatibility with older clients.
            email = data.get('email')
            symbols = data.get('symbols')  # Expecting an array of symbols

            # Validate input
            if not email or not symbols:
                return JsonResponse({"error": "email and symbols are required"}, status=400)
            if not isinstance(symbols, list):
                return JsonResponse({"error": "Symbols must be a list"}, status=400)

            # Prepare and execute bulk insertion (parameterized; token column
            # intentionally omitted — Security Gate 1 #19)
            for symbol in symbols:
                client.command(
                    """
                    INSERT INTO symbols (email, symbol, created_at) 
                    VALUES (%(email)s, %(symbol)s, NOW())
                    """,
                    {"email": email, "symbol": symbol},
                )

            return JsonResponse({"message": "Data saved successfully"}, status=200)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

# Getting symbols that the user selected
@swagger_auto_schema(
    method='GET',
    query_serializer=EmailSerializer,  # Use query_serializer for GET requests
    responses={200: openapi.Response('Success')}
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def get_symbol(request):
    if request.method == 'GET':
        try:
            client = get_clickhouse_client()
            email = request.query_params.get('email')

            if not email:
                return JsonResponse({"error": "email is required"}, status=400)

            # Ensure table exists (no rows → empty result, not a crash)
            client.command("""
                CREATE TABLE IF NOT EXISTS symbols (
                    email String,
                    token String,
                    symbol String,
                    created_at DateTime
                ) ENGINE = MergeTree()
                ORDER BY (email);
            """)

            result = client.query(
                """
                SELECT symbol
                FROM symbols
                WHERE email = %(email)s
                """,
                {"email": email},
            )
            symbol = result.result_set if result else []
            return JsonResponse({"symbol": symbol}, status=200)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

# dELETING SYMBOLS FROM THE DATABASE from the database
@swagger_auto_schema(method='DELETE', 
    request_body=openapi.Schema(
        type=openapi.TYPE_OBJECT,
        properties={
            'email': openapi.Schema(type=openapi.TYPE_STRING),
            'symbol': openapi.Schema(type=openapi.TYPE_STRING),
        }
    )
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def delete_symbol(request):
    if request.method == 'DELETE':
        try:
            client = get_clickhouse_client()
            data = json.loads(request.body)
            email = data.get('email')
            symbol = data.get('symbol') or data.get('index')

            if not email:
                return JsonResponse({"error": "email is required"}, status=400)

            # Ensure table exists
            client.command("""
                CREATE TABLE IF NOT EXISTS symbols (
                    email String,
                    token String,
                    symbol String,
                    created_at DateTime
                ) ENGINE = MergeTree()
                ORDER BY (email);
            """)

            client.command(
                """
                ALTER TABLE symbols DELETE WHERE symbol = %(symbol)s AND email = %(email)s
                """,
                {"symbol": symbol, "email": email},
            )
            return JsonResponse({"message": "deleted"}, status=200)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

# delete candles from database
@swagger_auto_schema(method='DELETE', 
    request_body=openapi.Schema(
        type=openapi.TYPE_OBJECT,
        properties={
            'symbol': openapi.Schema(type=openapi.TYPE_STRING),
        }
    )
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def delete_candles(request):
    if request.method == 'DELETE':
        try:
            client = get_clickhouse_client()
            # Parse JSON data from the request
            data = json.loads(request.body)
            symbol = data.get('symbol')

            if not symbol:
                return JsonResponse({"error": "symbol is required"}, status=400)

            # Resolve the symbol through a fixed allowlist — the raw value is
            # NEVER interpolated into SQL (arbitrary table access).
            symbol_table_map = {
                "EUR/USD": "eurousd",
                "Gold/USD": "gold_candles",
                "V75": "v75_candles",
                "US30": "us30_candles",
            }
            table_name = symbol_table_map.get(symbol)
            if table_name is None:
                return JsonResponse({"error": "symbol not supported"}, status=400)

            # table_name comes from the fixed allowlist above — safe to insert.
            result = client.query(
                "TRUNCATE TABLE {table_name};".format(table_name=table_name)
            )
            if result:
                response = result.result_set
                return JsonResponse({" Data deleted successfully, response ": response}, status=200)
            else:
                return JsonResponse({"error": "symbol not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

""" 
    Risk analysis setting setthings

    per trade risk and per day risk
    
"""
@swagger_auto_schema(method='post', 
    operation_id='Save risk percentage', 
    description="### Save Risk Percentage\nThis endpoint is used to save the risk percentage settings for trading strategies." ,
    request_body=openapi.Schema(
        type=openapi.TYPE_OBJECT,
        
        properties={
            'email': openapi.Schema(type=openapi.TYPE_STRING),
            'per_trade ': openapi.Schema(type=openapi.TYPE_NUMBER),
            'per_day ': openapi.Schema(type=openapi.TYPE_NUMBER),
        }
    )
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def save_risks(request):
    if request.method == 'POST':
        try:
            client = get_clickhouse_client()
            # Ensure the table exists
            client.command("""
                CREATE TABLE IF NOT EXISTS risk_table (
                    email String,
                    per_trade Float32,
                    per_day Float32,
                    created_at DateTime
                ) ENGINE = MergeTree()
                ORDER BY (email);
            """)

            # Parse JSON data from the request
            data = json.loads(request.body)
            day = data.get('per_day')
            email = data.get('email')
            trade = data.get('per_trade')

            # Validate input
            if not email or day is None:
                return JsonResponse({"error": "Email, day, and trade risks are required"}, status=400)
            
            if trade is None:
                trade = 1
                # setting default risk per trade to 1

            # Fetch existing risks for the user
            result = client.query(
                "SELECT per_trade, per_day FROM risk_table WHERE email = %(email)s",
                {"email": email}
            )
            row_data = result.result_set
            exists = len(row_data) > 0

            changes = {}

            if exists:
                # Extract the current risks
                existing_trade, existing_day = row_data[0]

                # Compare and update if necessary
                if float(existing_trade) != float(trade):
                    changes["per_trade"] = {"old": existing_trade, "new": trade}
                    client.command(
                        "ALTER TABLE risk_table UPDATE per_trade = %(trade)s WHERE email = %(email)s",
                        {"trade": trade, "email": email}
                    )

                if float(existing_day) != float(day):
                    changes["per_day"] = {"old": existing_day, "new": day}
                    client.command(
                        "ALTER TABLE risk_table UPDATE per_day = %(day)s WHERE email = %(email)s",
                        {"day": day, "email": email}
                    )
            else:
                # Insert new risks for the user if they don't exist
                client.command(
                    """
                    INSERT INTO risk_table (email, per_trade, per_day, created_at)
                    VALUES (%(email)s, %(trade)s, %(day)s, NOW())
                    """,
                    {"email": email, "trade": trade, "day": day}
                )
                changes["new_entry"] = {"per_trade": trade, "per_day": day}

            if changes:
                # Notify the user about what has changed
                return JsonResponse({"message": "Risks updated successfully", "changes": changes}, status=200)

            return JsonResponse({"message": "No changes were made, risks are already up to date"}, status=200)

        except Exception as e:
            # Handle server errors
            return JsonResponse({"error": f"Internal server error: {str(e)}"}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

# getting risk data from database
@swagger_auto_schema(
    method='GET',
    query_serializer=EmailSerializer,  # Use query_serializer for GET requests
    responses={200: openapi.Response('Success')}
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def get_risks(request):
    if request.method == 'GET':
        try:
            client = get_clickhouse_client()
            # Access query parameters
            email = request.query_params.get('email')

            if not email:
                return JsonResponse({"error": "email is required"}, status=400)

            # Fetch the strategy from the database (parameterized query)
            result = client.query(
                """
                SELECT per_trade, per_day
                FROM risk_table
                WHERE email = %(email)s
                """,
                {"email": email},
            )
            if result:
                risks = result.result_set[0]
                return JsonResponse({"risks": risks}, status=200)
            else:
                return JsonResponse({"error": "email not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

"""
    profit and loss marging 

    saving time frame when the bot shall stop executing 
    maximun loss per day
    and overall

    maximum win per day and overall
"""
# Define parameters for swagger documentation
email_param = openapi.Parameter('email', openapi.IN_BODY, description="User's email", type=openapi.TYPE_STRING)
max_loss_per_day_param = openapi.Parameter('max_loss_per_day', openapi.IN_BODY, description="Maximum loss per day", type=openapi.TYPE_NUMBER)
overall_loss_param = openapi.Parameter('overall_loss', openapi.IN_BODY, description="Overall loss", type=openapi.TYPE_NUMBER)
max_win_per_day_param = openapi.Parameter('max_win_per_day', openapi.IN_BODY, description="Maximum win per day", type=openapi.TYPE_NUMBER)
overall_win_param = openapi.Parameter('overall_win', openapi.IN_BODY, description="Overall win", type=openapi.TYPE_NUMBER)
start_date_param = openapi.Parameter('start_date', openapi.IN_BODY, description="Start date of the trading", type=openapi.TYPE_STRING)
stop_date_param = openapi.Parameter('stop_date', openapi.IN_BODY, description="Stop date of the trading", type=openapi.TYPE_STRING)

@swagger_auto_schema(method='post', 
    request_body=openapi.Schema(
        type=openapi.TYPE_OBJECT,
        properties={
            'email': openapi.Schema(type=openapi.TYPE_STRING),
            'max_loss_per_day': openapi.Schema(type=openapi.TYPE_NUMBER),
            'overall_loss': openapi.Schema(type=openapi.TYPE_NUMBER),
            'max_win_per_day': openapi.Schema(type=openapi.TYPE_NUMBER),
            'overall_win': openapi.Schema(type=openapi.TYPE_NUMBER),
            'start_date': openapi.Schema(type=openapi.TYPE_STRING),
            'stop_date': openapi.Schema(type=openapi.TYPE_STRING)
        }
    )
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def profit_and_loss_margin(request):
    if request.method == 'POST':
        try:
            client = get_clickhouse_client()
            # Ensure the table exists
            client.command("""
                CREATE TABLE IF NOT EXISTS start_stop_table (
                    email String,
                    start_date DateTime,
                    stop_date DateTime,
                    loss_per_day Float32,
                    overall_loss Float32,
                    win_per_day Float32,
                    overall_win Float32,
                    created_at DateTime
                ) ENGINE = MergeTree()
                ORDER BY (email);
            """)


            #Parse JSON data from the request
            data = json.loads(request.body)

            email = data.get('email')
            loss_per_day = data.get('max_loss_per_day')
            overall_loss = data.get('overall_loss')
            win_per_day = data.get('max_win_per_day')
            overall_win = data.get('overall_win')
            start_date = data.get('start_date')
            stop_date = data.get('stop_date')

            # Validate input
            if not email or not  stop_date or loss_per_day is None or overall_loss is None or win_per_day is None or overall_win is None:
                return JsonResponse({
                    "error": "Email, start_date, loss_per_day, overall_loss, win_per_day, and overall_win are required"
                }, status=400)

            # Fetch existing details for the user (parameterized query)
            result = client.query(
                """
                SELECT start_date, stop_date, loss_per_day, overall_loss, win_per_day, overall_win
                FROM start_stop_table
                WHERE email = %(email)s
                """,
                {"email": email},
            )
            # print(result.result_set)
            changes = {}

            if result :
                if result.result_set:
                    row_data = result.result_set[0]

                    # Extract the current settings
                    existing_start_date = row_data[0]
                    existing_stop_date = row_data[1]
                    existing_loss_per_day = row_data[2]
                    existing_overall_loss = row_data[3] 
                    existing_win_per_day = row_data[4]
                    existing_overall_win = row_data[5]

                    # Compare and update if necessary
                    if existing_start_date != start_date:
                        changes["start_date"] = {"old": str(existing_start_date), "new": str(start_date)}
                        client.command(
                            "ALTER TABLE start_stop_table UPDATE start_date = %(start_date)s WHERE email = %(email)s",
                            {"start_date": start_date, "email": email}
                        )

                    if existing_stop_date != stop_date:
                        changes["stop_date"] = {"old": str(existing_stop_date), "new": str(stop_date)}
                        client.command(
                            "ALTER TABLE start_stop_table UPDATE stop_date = %(stop_date)s WHERE email = %(email)s",
                            {"stop_date": stop_date, "email": email}
                        )


                    if float(existing_loss_per_day) != float(loss_per_day):
                        changes["loss_per_day"] = {"old": existing_loss_per_day, "new": loss_per_day}
                        client.command(
                            "ALTER TABLE start_stop_table UPDATE loss_per_day = %(loss_per_day)s WHERE email = %(email)s",
                            {"loss_per_day": loss_per_day, "email": email}
                        )

                    if float(existing_overall_loss) != float(overall_loss):
                        changes["overall_loss"] = {"old": existing_overall_loss, "new": overall_loss}
                        client.command(
                            "ALTER TABLE start_stop_table UPDATE overall_loss = %(overall_loss)s WHERE email = %(email)s",
                            {"overall_loss": overall_loss, "email": email}
                        )

                    if float(existing_win_per_day) != float(win_per_day):
                        changes["win_per_day"] = {"old": existing_win_per_day, "new": win_per_day}
                        client.command(
                            "ALTER TABLE start_stop_table UPDATE win_per_day = %(win_per_day)s WHERE email = %(email)s",
                            {"win_per_day": win_per_day, "email": email}
                        )

                    if float(existing_overall_win) != float(overall_win):
                        changes["overall_win"] = {"old": existing_overall_win, "new": overall_win}
                        client.command(
                            "ALTER TABLE start_stop_table UPDATE overall_win = %(overall_win)s WHERE email = %(email)s",
                            {"overall_win": overall_win, "email": email}
                        )
                else:
                    # Insert new details for the user if they don't exist
                    # (parameterized query)
                    client.query(
                        """
                        INSERT INTO start_stop_table (email, start_date, stop_date, loss_per_day, overall_loss, win_per_day, overall_win, created_at)
                        VALUES (%(email)s, %(start_date)s, %(stop_date)s, %(loss_per_day)s, %(overall_loss)s, %(win_per_day)s, %(overall_win)s, NOW())
                        """,
                        {
                            "email": email,
                            "start_date": start_date,
                            "stop_date": stop_date,
                            "loss_per_day": loss_per_day,
                            "overall_loss": overall_loss,
                            "win_per_day": win_per_day,
                            "overall_win": overall_win,
                        },
                    )
                    changes["new_entry"] = {
                        "start_date": str(start_date),
                        "loss_per_day": loss_per_day,
                        "overall_loss": overall_loss,
                        "win_per_day": win_per_day,
                        "overall_win": overall_win
                    }

                if changes:
                    # Notify the user about what has changed
                    return JsonResponse({"message": "Profit and loss margins updated successfully", "changes": changes}, status=200)

                return JsonResponse({"message": "No changes were made, settings are already up to date"}, status=200)
            else:
                return JsonResponse({"message": "No changes were made"}, status=300)
        except Exception as e:
            # Handle server errors
            return JsonResponse({"error": f"Internal server error: {str(e)}"}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

# getting profit and loss data data from database
@csrf_exempt
@swagger_auto_schema(
    method='GET',
    query_serializer=EmailSerializer,  # Use query_serializer for GET requests
    responses={200: openapi.Response('Success')}
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_profit_and_loss_margin(request):
    if request.method == 'GET':
        try:
            client = get_clickhouse_client()
            email = request.query_params.get('email')

            if not email:
                return JsonResponse({"error": "email is required"}, status=400)

            result = client.query(
                """
                SELECT start_date, stop_date, loss_per_day, overall_loss, win_per_day, overall_win
                FROM start_stop_table
                WHERE email = %(email)s
                """,
                {"email": email},
            )
            
            if result :
                data = result.result_set[0]
                return JsonResponse({"data": data}, status=200)
            else:
                return JsonResponse({"error": "email not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

"""
Get candles from backend
"""
class SymbolSerializer(serializers.Serializer):
    symbol = serializers.CharField(max_length=10)
    
@swagger_auto_schema(
    method='GET',
    query_serializer=SymbolSerializer,  # Use query_serializer for GET requests
    responses={200: openapi.Response('Success')}
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def get_candles(request):
    if request.method == 'GET':
        try:
            client = get_clickhouse_client()
            # Access query parameters
            symbol = request.query_params.get('symbol')

            if not symbol:
                return JsonResponse({"error": "symbol is required"}, status=400)

            # Resolve the symbol through a fixed allowlist — the raw value is
            # NEVER interpolated into SQL (previously allowed reading ANY
            # table, e.g. userdetails.token).
            symbol_table_map = {
                "EUR/USD": "eurousd",
                "Gold/USD": "gold_candles",
                "V75": "v75_candles",
                "US30": "us30_candles",
            }
            table_name = symbol_table_map.get(symbol)
            if table_name is None:
                return JsonResponse({"error": "symbol not supported"}, status=400)

            # table_name comes from the fixed allowlist above — safe to insert.
            result = client.query(
                """
                WITH ranked_data AS (
                SELECT 
                    toTimeZone(timestamp, 'Africa/Maputo') AS timestamp, 
                    open, 
                    high, 
                    low, 
                    close,
                    ROW_NUMBER() OVER (PARTITION BY timestamp ORDER BY timestamp ASC) AS rn
                FROM {table_name}
                WHERE timestamp <= toTimeZone(now(), 'Africa/Maputo') -- Filter up to current time in CAT
            )
            SELECT timestamp, open, high, low, close
            FROM ranked_data
            WHERE rn = 1
            ORDER BY timestamp ASC
                """.format(table_name=table_name)
            )


            if result:
                data = result.result_set
                # print(data)
                cat_timezone = pytz.timezone('Africa/Harare')

                formatted_data = []
                for entry in data:
                    dt_naive = datetime.strptime(entry[0].strftime("%Y-%m-%d %H:%M:%S"), "%Y-%m-%d %H:%M:%S")

                    # Attach the CAT timezone to the datetime object
                    dt_cat = cat_timezone.localize(dt_naive)

                    # Convert the timezone-aware datetime to a Unix timestamp
                    timestamp = int(dt_cat.timestamp())

                    formatted_data.append({
                        "time": timestamp,
                        "open": entry[1],
                        "high": entry[2],
                        "low": entry[3],
                        "close": entry[4],
                    })

                # Converting to JSON format for use in chat
                json_data = json.dumps(formatted_data, indent=4)
                # print(json_data)
                return JsonResponse({"data": json_data}, status=200)
            else:
                return JsonResponse({"error": "Token not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)

@swagger_auto_schema(
    method='GET',
    query_serializer=SymbolSerializer,  # Use query_serializer for GET requests
    responses={200: openapi.Response('Success')}
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def get_latest_candle(request):
    if request.method == 'GET':
        try:
            client = get_clickhouse_client()
            # Access query parameters
            symbol = request.query_params.get('symbol')

            if not symbol:
                return JsonResponse({"error": "Symbol is required"}, status=400)

            # Resolve the symbol through a fixed allowlist — the raw value is
            # NEVER interpolated into SQL (previously allowed reading ANY
            # table, e.g. userdetails.token).
            symbol_table_map = {
                "EUR/USD": "eurousd",
                "Gold/USD": "gold_candles",
                "V75": "v75_candles",
                "US30": "us30_candles",
            }
            table_name = symbol_table_map.get(symbol)
            if table_name is None:
                return JsonResponse({"error": "symbol not supported"}, status=400)

            # Fetch the latest candle from the database (allowlisted table)
            result = client.query(
                """
                SELECT 
                    toTimeZone(timestamp, 'Africa/Harare') AS timestamp, 
                    open, 
                    high, 
                    low, 
                    close
                FROM {table_name}
                ORDER BY timestamp DESC
                LIMIT 1
                """.format(table_name=table_name)
            )

            if result:
                data = result.result_set
                if data:
                    entry = data[0]  # Get the first (and only) row

                    # Convert the timestamp to a Unix timestamp
                    dt_naive = datetime.strptime(entry[0].strftime("%Y-%m-%d %H:%M:%S"), "%Y-%m-%d %H:%M:%S")
                    cat_timezone = pytz.timezone('Africa/Maputo')
                    dt_cat = cat_timezone.localize(dt_naive)
                    timestamp = int(dt_cat.timestamp())

                    # Format the candle data
                    candle = {
                        "time": timestamp,
                        "open": entry[1],
                        "high": entry[2],
                        "low": entry[3],
                        "close": entry[4],
                    }

                    return JsonResponse({"data": candle}, status=200)
                else:
                    return JsonResponse({"error": "No data found"}, status=404)
            else:
                return JsonResponse({"error": "Query failed"}, status=500)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)


# saving subscribbers
@swagger_auto_schema(method='post', 
    operation_id='Subscribe', 
    description="### Subscribe\nThis endpoint is used to save user email to the database for subscribing." ,
    request_body=openapi.Schema(
        type=openapi.TYPE_OBJECT,
        properties={
            'email': openapi.Schema(type=openapi.TYPE_STRING),
        }
    )
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def Subscribe(request):
    if request.method == 'POST':
        try:
            client = get_clickhouse_client()

            # Ensure the table exists
            client.command("""
                CREATE TABLE IF NOT EXISTS subscribers(
                    email String,
                    created_at DateTime
                ) ENGINE = MergeTree()
                ORDER BY (email);
            """)

            # Parse JSON data from the request
            data = json.loads(request.body)
            email = data.get('email')

            if email is None:
                return JsonResponse({"error": "Email is required"}, status=400)

            # Check if already subscribed
            result = client.query(
                "SELECT COUNT(*) FROM subscribers WHERE email = %(email)s",
                {"email": email}
            )
            exists = result.result_set[0][0] > 0

            if exists:
                return JsonResponse({"message": "Subscriber already up to date"}, status=200)
            else:
                # Insert new subscriber
                client.command(
                    """
                    INSERT INTO subscribers (email, created_at)
                    VALUES (%(email)s, NOW())
                    """,
                    {"email": email}
                )

                # Send confirmation email
                subject = "Subscription Confirmed"
                message = "Thank you for subscribing to our updates!"
                send_result = send_email(email, subject, message)

                return JsonResponse({
                    "message": "Subscribed successfully",
                    "email_status": send_result
                }, status=200)

        except Exception as e:
            return JsonResponse({"error": f"Internal server error: {str(e)}"}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)
# Deleting subscribe option
@swagger_auto_schema(method='DELETE', 
    request_body=openapi.Schema(
        type=openapi.TYPE_OBJECT,
        properties={
            'email': openapi.Schema(type=openapi.TYPE_STRING),
        }
    )
)
@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def Unsubscribe(request):
    if request.method == 'DELETE':
        try:
            client = get_clickhouse_client()
            # Parse JSON data from the request
            data = json.loads(request.body)
            email = data.get('email')

            if not email:
                return JsonResponse({"error": "email is required"}, status=400)

            # Deleting users from database (parameterized query)
            result = client.query(
                """
                ALTER TABLE subscribers DELETE WHERE email = %(email)s
                """,
                {"email": email},
            )
            if result:
                email = result.result_set
                return JsonResponse({"Unsubscribed Successfully"}, status=200)
            else:
                return JsonResponse({"error": "Email not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)


# ── Symbols Catalogue (DB-backed, UI-managed) ─────────────────────────
import os

def _ensure_catalogue_table(client):
    """Create symbols_catalogue table if it doesn't exist."""
    client.command("""
        CREATE TABLE IF NOT EXISTS symbols_catalogue (
            symbol String,
            name String DEFAULT '',
            description String DEFAULT '',
            spread String DEFAULT '',
            volatility String DEFAULT '',
            best_time String DEFAULT '',
            created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree()
        ORDER BY (symbol)
    """)

def _seed_catalogue_from_env(client):
    """Seed catalogue from TRADING_ASSETS env var (idempotent — skips existing)."""
    assets_raw = os.environ.get("TRADING_ASSETS", "")
    if not assets_raw:
        return
    try:
        assets = json.loads(assets_raw)
        if not isinstance(assets, list):
            return
    except (json.JSONDecodeError, ValueError):
        return

    for asset in assets:
        sym = asset.get("symbol", "").strip().upper()
        if not sym:
            continue
        result = client.query(
            "SELECT symbol FROM symbols_catalogue WHERE symbol = %(sym)s",
            {"sym": sym},
        )
        if result.result_set:
            continue
        client.command(
            """INSERT INTO symbols_catalogue (symbol, name, description, spread, volatility, best_time)
               VALUES (%(symbol)s, %(name)s, %(description)s, %(spread)s, %(volatility)s, %(best_time)s)""",
            {
                "symbol": sym,
                "name": asset.get("name", ""),
                "description": asset.get("description", ""),
                "spread": asset.get("spread", ""),
                "volatility": asset.get("volatility", ""),
                "best_time": asset.get("best_time", ""),
            },
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@csrf_exempt
def symbols_catalogue_list(request):
    """
    GET /api/symbols/catalogue/
    Returns all symbols in the catalogue. Auto-seeds from env on first call.
    Contract: {"symbols": [{"symbol": "EURUSD", "name": "", ...}, ...]}
    """
    try:
        client = get_clickhouse_client()
        _ensure_catalogue_table(client)
        _seed_catalogue_from_env(client)
        result = client.query("SELECT symbol, name, description, spread, volatility, best_time FROM symbols_catalogue ORDER BY symbol")
        symbols = [
            {
                "symbol": row[0],
                "name": row[1],
                "description": row[2],
                "spread": row[3],
                "volatility": row[4],
                "best_time": row[5],
            }
            for row in result.result_set
        ]
        return JsonResponse({"symbols": symbols}, status=200)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@csrf_exempt
def symbols_catalogue_add(request):
    """
    POST /api/symbols/catalogue/
    Add a symbol to the catalogue. Body: {"symbol": "BTCUSD", "name": "Bitcoin", ...}
    All fields except 'symbol' are optional.
    """
    try:
        client = get_clickhouse_client()
        _ensure_catalogue_table(client)
        data = json.loads(request.body)
        sym = (data.get("symbol") or "").strip().upper()
        if not sym:
            return JsonResponse({"error": "symbol is required"}, status=400)

        existing = client.query(
            "SELECT symbol FROM symbols_catalogue WHERE symbol = %(sym)s",
            {"sym": sym},
        )
        if existing.result_set:
            return JsonResponse({"error": f"{sym} already exists"}, status=409)

        client.command(
            """INSERT INTO symbols_catalogue (symbol, name, description, spread, volatility, best_time)
               VALUES (%(symbol)s, %(name)s, %(description)s, %(spread)s, %(volatility)s, %(best_time)s)""",
            {
                "symbol": sym,
                "name": data.get("name", ""),
                "description": data.get("description", ""),
                "spread": data.get("spread", ""),
                "volatility": data.get("volatility", ""),
                "best_time": data.get("best_time", ""),
            },
        )
        return JsonResponse({"message": f"{sym} added"}, status=201)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
@csrf_exempt
def symbols_catalogue_delete(request):
    """
    DELETE /api/symbols/catalogue/
    Remove a symbol from the catalogue. Body: {"symbol": "BTCUSD"}
    """
    try:
        client = get_clickhouse_client()
        _ensure_catalogue_table(client)
        data = json.loads(request.body)
        sym = (data.get("symbol") or "").strip().upper()
        if not sym:
            return JsonResponse({"error": "symbol is required"}, status=400)

        client.command(
            "ALTER TABLE symbols_catalogue DELETE WHERE symbol = %(sym)s",
            {"sym": sym},
        )
        return JsonResponse({"message": f"{sym} removed"}, status=200)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
