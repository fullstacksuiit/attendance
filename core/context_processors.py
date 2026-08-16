def organization(request):
    """Expose the logged-in admin's organization to every template."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return {"current_org": user.organization}
    return {"current_org": None}
