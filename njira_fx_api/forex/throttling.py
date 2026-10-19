from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from forex.dashboard_view import SESSION_KEY


class DashboardReadThrottleMixin:
    def allow_request(self, request, view):
        if request.method in ("GET", "HEAD") and request.session.get(SESSION_KEY):
            return True
        return super().allow_request(request, view)


class DashboardReadAnonThrottle(DashboardReadThrottleMixin, AnonRateThrottle):
    pass


class DashboardReadUserThrottle(DashboardReadThrottleMixin, UserRateThrottle):
    pass
