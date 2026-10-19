from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.core.mail import EmailMessage, send_mail
from django.conf import settings
from forex.clickhouse.connection import get_clickhouse_client
from datetime import datetime
import json
import uuid

# JWT auth for plain Django views
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

# Send an actual email
def send_email(address, subject, message, attachment=None):
    try:
        print("sending email to ", address)
        if attachment is None:
            send_mail(subject, message, settings.EMAIL_HOST_USER, [address])
        else:
            email = EmailMessage(subject, message, settings.EMAIL_HOST_USER, [address])
            email.attach_file(attachment)
            email.send(fail_silently=False)
        save_notification(address, subject, message)
        return {"success": True, "message": "Email sent successfully"}
    except Exception as e:
        print(f"Error sending email: {e}")
        return {"success": False, "message": f"Error sending email: {e}"}

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def dummy_send_email(request):
    # Dummy values
    dummy_email = "mkalidozo3@gmail.com"
    dummy_subject = "Test Email"
    dummy_message = "This is a test email from the dummy endpoint."

    result = send_email(dummy_email, dummy_subject, dummy_message)
    return JsonResponse(result)

# Save notification to ClickHouse with UUID
def save_notification(address, subject, message):
    try:
        client = get_clickhouse_client()

        # client.command("DROP TABLE IF EXISTS notifications;")

        # Create table with UUID column if not exists
        client.command("""
            CREATE TABLE IF NOT EXISTS notifications (
                id UUID,
                email String,
                subject String,
                message String,
                timestamp DateTime
            ) ENGINE = MergeTree()
            ORDER BY (email, timestamp);
        """)

        notification_id = str(uuid.uuid4())

        insert_query = f"""
            INSERT INTO notifications (id, email, subject, message, timestamp)
            VALUES ('{notification_id}', '{address}', '{subject}', '{message}', NOW());
        """
        client.command(insert_query)

        return {"success": True, "message": "Notification saved successfully", "id": notification_id}
    except Exception as e:
        print(f"Error saving notification: {e}")
        return {"success": False, "message": f"Error saving notification: {e}"}

# API endpoint to send email and save notification
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
@require_http_methods(["POST"])
def send_email_api(request):
    try:
        body = json.loads(request.body)
        address = body.get("email")
        subject = body.get("subject", "No Subject")
        message = body.get("message", "No Message")

        if not address:
            return JsonResponse({"success": False, "message": "Email address is required"}, status=400)

        email_response = send_email(address, subject, message)

        if email_response["success"]:
            save_response = save_notification(address, subject, message)
            if not save_response["success"]:
                print(f"Save notification failed: {save_response['message']}")
                return JsonResponse({
                    "email": email_response,
                    "save_notification": save_response
                }, status=207)

        return JsonResponse(email_response, status=200 if email_response['success'] else 500)

    except Exception as e:
        print(f"Unexpected error in send_email_api: {e}")
        return JsonResponse({"success": False, "message": f"Unexpected error: {e}"}, status=500)

# API to fetch notifications
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
@require_http_methods(["POST"])
def fetch_notifications_by_email(request):
    try:
        body = json.loads(request.body)
        email = body.get("email")

        if not email:
            return JsonResponse({"success": False, "message": "Email is required"}, status=400)

        client = get_clickhouse_client()
        query = f"""
            SELECT id, subject, message, timestamp 
            FROM notifications 
            WHERE email = '{email}' 
            ORDER BY timestamp DESC
        """
        result = client.query(query)
        data = [
            {
                "id": row[result.column_names.index("id")],
                "subject": row[result.column_names.index("subject")],
                "message": row[result.column_names.index("message")],
                "timestamp": row[result.column_names.index("timestamp")].strftime("%Y-%m-%d %H:%M:%S"),
            }
            for row in result.result_set
        ]

        return JsonResponse({"success": True, "notifications": data}, status=200)

    except Exception as e:
        print(f"Error fetching notifications: {e}")
        return JsonResponse({"success": False, "message": f"Error fetching notifications: {e}"}, status=500)

# API to delete notifications
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
@require_http_methods(["POST"])
def delete_notifications_by_email(request):
    try:
        body = json.loads(request.body)
        email = body.get("email")

        if not email:
            return JsonResponse({"success": False, "message": "Email is required"}, status=400)

        client = get_clickhouse_client()
        client.command(
            "ALTER TABLE notifications DELETE WHERE email = %(email)s",
            {"email": email},
        )

        return JsonResponse({"success": True, "message": "Notifications deleted successfully"}, status=200)

    except Exception as e:
        print(f"Error deleting notifications: {e}")
        return JsonResponse({"success": False, "message": f"Error deleting notifications: {e}"}, status=500)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
@csrf_exempt
@require_http_methods(["POST"])
def delete_notification_by_id(request):
    try:
        body = json.loads(request.body)
        notification_id = body.get("id")

        if not notification_id:
            return JsonResponse({"success": False, "message": "Notification ID is required"}, status=400)

        client = get_clickhouse_client()
        client.command(
            "ALTER TABLE notifications DELETE WHERE id = %(notification_id)s",
            {"notification_id": notification_id},
        )

        return JsonResponse({"success": True, "message": f"Notification with ID {notification_id} deleted successfully"}, status=200)

    except Exception as e:
        print(f"Error deleting notification by ID: {e}")
        return JsonResponse({"success": False, "message": f"Error deleting notification: {e}"}, status=500)
