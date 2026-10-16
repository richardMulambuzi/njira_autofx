from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

# Define the schema view for the Swagger UI
schema_view = get_schema_view(
    openapi.Info(
        title="Forex Trading", 
        default_version='v1', 
        description="Njira Forex Bot is a comprehensive and powerful forex trading platform designed to automate trading strategies, manage forex accounts, and provide in-depth analysis of the forex market. This bot enables users to execute trades, monitor their account performance, and gain valuable insights through real-time data and visualizations. ",  
        terms_of_service="https://www.google.com/policies/terms/",  # Terms of service URL
        contact=openapi.Contact(email="95221011+RichardMulambuzi@users.noreply.github.com"),  # Contact information
        license=openapi.License(name="BSD License"),  # License type
    ),
    public=True,  # This allows the schema to be public (accessible without authentication)
    permission_classes=(permissions.AllowAny,),  # This sets the permission to allow anyone to view the schema
)
