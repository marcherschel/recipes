"""
Single tenant mode.

Tandoor is built around multiple isolated "spaces". For a self hosted instance shared between friends/family this
is mostly in the way: every new (social/LDAP/remote) user lands in a fresh space of their own.

When ``SINGLE_TENANT_ID`` is configured
  * every user is forced to be a member of that space (and only that space)
  * every other space is handed over to an inactive placeholder user so no data is lost
  * newly created users (local signup, social signup, LDAP, remote user, admin, ...) join that space automatically
  * creating additional spaces / joining other spaces is disabled

``SINGLE_TENANT_ID`` is mandatory for starting the server. If it is missing or invalid startup is aborted and all
existing spaces are listed so the admin can pick one.
"""
import logging

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.exceptions import ImproperlyConfigured
from django.core.management.color import no_style
from django.db import DatabaseError, connection, transaction
from django_scopes import scopes_disabled

logger = logging.getLogger(__name__)

PLACEHOLDER_USERNAME_PREFIX = 'single-tenant-placeholder-space-'


class SingleTenantConfigurationError(ImproperlyConfigured):
    pass


def is_single_tenant():
    return bool(getattr(settings, 'SINGLE_TENANT_ID', None))


def get_single_tenant_id():
    """
    :return: configured space id as int or None if single tenant mode is not configured
    :raises SingleTenantConfigurationError: if the value is not an integer
    """
    value = getattr(settings, 'SINGLE_TENANT_ID', None)
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SingleTenantConfigurationError(f'SINGLE_TENANT_ID must be the numeric id of a space, got {value!r}.\n' + describe_spaces())


def placeholder_username(space_id):
    return f'{PLACEHOLDER_USERNAME_PREFIX}{space_id}'


def is_placeholder_user(user):
    return bool(user and user.username and user.username.startswith(PLACEHOLDER_USERNAME_PREFIX))


def real_users():
    return User.objects.exclude(username__startswith=PLACEHOLDER_USERNAME_PREFIX)


def describe_spaces():
    from cookbook.models import Space, UserSpace

    with scopes_disabled():
        try:
            spaces = list(Space.objects.select_related('created_by').order_by('pk'))
        except DatabaseError as e:
            return f'Could not list spaces (did you run migrations?): {e}'

        if not spaces:
            return ('No spaces exist yet. Set SINGLE_TENANT_ID to the id the space should get (e.g. SINGLE_TENANT_ID=1); '
                    'it will be created for the first user that signs up / is created.')

        lines = ['Existing spaces:', f'  {"ID":>6}  {"MEMBERS":>7}  {"OWNER":<36}  NAME']
        for s in spaces:
            owner = s.created_by.username if s.created_by else '-'
            members = UserSpace.objects.filter(space=s).exclude(user__username__startswith=PLACEHOLDER_USERNAME_PREFIX).count()
            lines.append(f'  {s.pk:>6}  {members:>7}  {owner:<36}  {s.name}')
        lines.append('Set SINGLE_TENANT_ID to the ID of the space all users should share.')
        return '\n'.join(lines)


def _get_group(name):
    group = Group.objects.filter(name=name).first()
    if not group:
        raise SingleTenantConfigurationError(f'SINGLE_TENANT_DEFAULT_GROUP={name!r} does not match any group (expected one of guest, user, admin).')
    return group


def _member_group():
    return _get_group(settings.SINGLE_TENANT_DEFAULT_GROUP)


def get_single_tenant_space():
    from cookbook.models import Space
    space_id = get_single_tenant_id()
    if space_id is None:
        return None
    with scopes_disabled():
        return Space.objects.filter(pk=space_id).first()


def _create_single_tenant_space(owner):
    """ bootstrap: no space exists yet, create the configured one owned by the given (first) user """
    from cookbook.models import Space

    space_id = get_single_tenant_id()
    space = Space.objects.create(
        pk=space_id,
        name=f"{owner.username}'s Space",
        created_by=owner,
        max_file_storage_mb=settings.SPACE_DEFAULT_MAX_FILES,
        max_recipes=settings.SPACE_DEFAULT_MAX_RECIPES,
        max_users=settings.SPACE_DEFAULT_MAX_USERS,
        allow_sharing=settings.SPACE_DEFAULT_ALLOW_SHARING,
        ai_enabled=settings.SPACE_AI_ENABLED,
        ai_credits_monthly=settings.SPACE_AI_CREDITS_MONTHLY,
        space_setup_completed=False,
    )
    # explicit pk does not advance the postgres sequence, fix it so later inserts don't collide
    for sql in connection.ops.sequence_reset_sql(no_style(), [Space]):
        with connection.cursor() as cursor:
            cursor.execute(sql)
    return space


def _get_or_create_placeholder(space):
    user = User.objects.filter(username=placeholder_username(space.pk)).first()
    if not user:
        user = User(username=placeholder_username(space.pk), first_name='Placeholder', last_name=f'(space {space.pk})', is_active=False)
        user.set_unusable_password()
        user.save()
    return user


def _hand_over_space_to_placeholder(space):
    """ make sure a (non single tenant) space is owned by and only contains its placeholder user """
    from cookbook.models import UserSpace

    placeholder = _get_or_create_placeholder(space)
    if space.created_by_id != placeholder.pk:
        space.created_by = placeholder
        space.save(update_fields=['created_by'])

    us, created = UserSpace.objects.get_or_create(user=placeholder, space=space, defaults={'active': True})
    if created or not us.groups.exists():
        us.groups.add(_get_group('admin'))
    return placeholder


def _join(user, space, group=None):
    """ make user an active member of space and remove all other memberships """
    from cookbook.models import UserSpace

    us = UserSpace.objects.filter(user=user, space=space).first()
    if not us:
        us = UserSpace.objects.create(user=user, space=space, active=True)
        us.groups.add(group or _member_group())
    else:
        if not us.groups.exists():
            us.groups.add(group or _member_group())
        if not us.active:
            us.active = True
            us.save(update_fields=['active'])

    removed = UserSpace.objects.filter(user=user).exclude(pk=us.pk)
    removed_count = removed.count()
    if removed_count:
        removed.delete()
    return us, removed_count


def ensure_user_in_single_tenant(user):
    """
    Force user into the single tenant space. Spaces created by that user are handed over to a placeholder.
    Called for every newly created user and as safety net from the middleware.
    :return: the users UserSpace of the single tenant space or None if single tenant mode is off / user is a placeholder
    """
    if not is_single_tenant() or user is None or not user.pk or is_placeholder_user(user):
        return None

    from cookbook.models import Space

    with scopes_disabled(), transaction.atomic():
        space = Space.objects.select_for_update().filter(pk=get_single_tenant_id()).first()
        group = None
        if not space:
            if Space.objects.exists():
                raise SingleTenantConfigurationError(f'Space SINGLE_TENANT_ID={get_single_tenant_id()} does not exist.\n' + describe_spaces())
            space = _create_single_tenant_space(user)
            group = _get_group('admin')

        for own_space in Space.objects.filter(created_by=user).exclude(pk=space.pk):
            _hand_over_space_to_placeholder(own_space)

        us, _ = _join(user, space, group)
        return us


def enforce_single_tenant(stdout=None):
    """
    Startup routine. Validates configuration and migrates all users into the single tenant space.
    :raises SingleTenantConfigurationError: if SINGLE_TENANT_ID is unset, invalid or does not reference an existing space
    """
    from cookbook.models import Space

    def log(msg):
        if stdout:
            stdout.write(msg)
        logger.info(msg)

    space_id = get_single_tenant_id()
    if space_id is None:
        raise SingleTenantConfigurationError('The environment variable SINGLE_TENANT_ID is required but not set.\n' + describe_spaces())

    with scopes_disabled(), transaction.atomic():
        try:
            space = Space.objects.select_for_update().filter(pk=space_id).first()
            any_space = Space.objects.exists()
        except DatabaseError as e:
            raise SingleTenantConfigurationError(f'Could not query spaces, make sure migrations have been applied: {e}')

        if not space:
            if not any_space:
                users = list(real_users().order_by('pk'))
                if not users:
                    log(f'Single tenant: no spaces and no users yet, space {space_id} will be created for the first user.')
                    return
                # users without any space (e.g. created via createsuperuser): bootstrap with the first (super)user as owner
                owner = next((u for u in users if u.is_superuser), users[0])
                space = _create_single_tenant_space(owner)
                _join(owner, space, _get_group('admin'))
                log(f'Single tenant: created space {space_id} for {owner.username}.')
            else:
                raise SingleTenantConfigurationError(f'SINGLE_TENANT_ID={space_id} does not reference an existing space.\n' + describe_spaces())

        # hand every other space over to its placeholder user, this keeps the data but nobody real is member anymore
        for other in Space.objects.exclude(pk=space.pk):
            _hand_over_space_to_placeholder(other)

        joined = 0
        removed = 0
        for user in real_users():
            _, r = _join(user, space)
            joined += 1
            removed += r

        if space.max_users and joined > space.max_users:
            logger.warning(f'Single tenant: space {space.pk} has {joined} members but max_users={space.max_users}.')

        log(f'Single tenant: {joined} user(s) are members of space {space.pk} ({space.name}), removed {removed} other membership(s).')
        return space


def run_startup_check():
    """ called when the server process starts, prints a readable error and exits if misconfigured """
    import sys
    try:
        enforce_single_tenant()
    except SingleTenantConfigurationError as e:
        print(f'\nERROR: single tenant mode misconfigured, refusing to start.\n{e}\n', file=sys.stderr, flush=True)
        # 4 == gunicorn APP_LOAD_ERROR, makes the gunicorn arbiter shut down instead of respawning workers
        sys.exit(4)
