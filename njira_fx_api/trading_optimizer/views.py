from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
import json
from forex.clickhouse.connection import get_clickhouse_client
from .reciever import refine_signals  # This is the function you'll define in receiver.py


from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
import json
from .reciever import refine_signals

# swagger
# from rest_framework.decorators import api_view
# from rest_framework.response import Response
# from drf_yasg.utils import swagger_auto_schema
# from drf_yasg import openapi
# from rest_framework import serializers

# JWT auth for plain Django views
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def optimize_signal_view(request):
    if request.method == 'POST':
        try:
            # Parse JSON body
            data = json.loads(request.body)
            
            # Ensure it's a list
            if not isinstance(data, list):
                return JsonResponse(
                    {"error": "Input must be a list of signals"},
                    status=400
                )
            
            # Process signals
            optimized_results = refine_signals(data)
            
            return  optimized_results
            # return JsonResponse(
            #     {"results": optimized_results},
            #     status=200
            # )
            
        except json.JSONDecodeError:
            return JsonResponse(
                {"error": "Invalid JSON format"},
                status=400
            )
        except Exception as e:
            return JsonResponse(
                {"error": f"Server error: {str(e)}"},
                status=500
            )
    else:
        return JsonResponse(
            {"error": "Only POST requests are allowed"},
            status=405
        )


# Getting the time the user allowed the bot to start trading
# @swagger_auto_schema(
#     method='GET',
#     responses={200: openapi.Response('Success')}
# )
# @api_view(['GET'])
@api_view(['GET'])
@permission_classes([IsAuthenticated])
@csrf_exempt
def optimalTradeTime(request):
    if request.method == 'GET':
        try:
            client = get_clickhouse_client()
            # Fetch the start_time from the database (static query)
            result = client.query("""
                SELECT symbol, best_days, best_hours
                FROM optimal_time
                WHERE length(best_days) > 0
                LIMIT 1000
                """)

            
            if result:
                data = result.result_set
                print(data)
                return JsonResponse({"optimal_times": data}, status=200)
            else:
                return JsonResponse({"error": "times not found"}, status=404)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid HTTP method"}, status=405)
