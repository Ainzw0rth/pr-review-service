from django.urls import path

from . import views

urlpatterns = [
    path("healthz/", views.health, name="health"),
    path("", views.dashboard, name="dashboard"),
    path("login/", views.sign_in, name="login"),
    path("logout/", views.sign_out, name="logout"),
    path("access/<str:token>/", views.access, name="access"),
    path("jobs/<uuid:job_id>/", views.job_detail, name="job_detail"),
]
