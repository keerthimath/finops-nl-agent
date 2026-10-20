def is_admin_user(user, admin_users: tuple[str, ...], admin_group_ids: tuple[str, ...]) -> bool:
    if user is None:
        return False
    if user.identifier.lower() in admin_users:
        return True
    group_ids = (getattr(user, "metadata", None) or {}).get("groups", [])
    return bool({group_id.lower() for group_id in group_ids if isinstance(group_id, str)}
                & set(admin_group_ids))