from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from core.views import HomeLandingView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', HomeLandingView.as_view(), name='home'),
    path('maps/', include('core.urls')),
    path('accounts/', include('accounts.urls')),
    path('companies/', include('catalog.urls_companies')),
    path('products/', include('catalog.urls_products')),
    path('cart/', include('cart.urls')),
    path('orders/', include('orders.urls')),
]
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
