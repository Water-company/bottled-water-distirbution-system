from django.contrib import admin

from catalog.models import Agent, AgentStock, Company, Driver, Product, ProductImage


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "location", "verification_status", "is_verified", "admin")
    list_filter = ("verification_status", "is_verified", "location")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "location")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "price", "available_quantity", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("name", "company__name")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [ProductImageInline]


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "location_name", "service_radius_km", "is_active", "is_accepting_orders")
    list_filter = ("company", "is_active", "is_accepting_orders")
    search_fields = ("name", "company__name", "location_name")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(AgentStock)
class AgentStockAdmin(admin.ModelAdmin):
    list_display = ("agent", "product", "available_quantity", "reorder_level")
    list_filter = ("agent__company", "agent")
    search_fields = ("agent__name", "product__name", "product__company__name")


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ("user", "agent", "vehicle_identifier", "is_active")
    list_filter = ("agent__company", "agent", "is_active")
    search_fields = ("user__first_name", "user__last_name", "user__email", "agent__name")

# Register your models here.
