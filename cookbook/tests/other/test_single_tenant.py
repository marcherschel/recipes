from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, User
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from django_scopes import scopes_disabled

from cookbook.forms import AllAuthSocialSignupForm
from cookbook.helper.permission_helper import create_space_for_user
from cookbook.helper.single_tenant import (PLACEHOLDER_USERNAME_PREFIX, SingleTenantConfigurationError, describe_spaces, enforce_single_tenant,
                                           placeholder_username)
from cookbook.models import InviteLink, Keyword, Space, UserSpace

pytestmark = pytest.mark.django_db


def groups(us):
    return set(us.groups.values_list('name', flat=True))


@pytest.fixture
def legacy():
    """ multi tenant state as it exists before switching: everybody has an own space, bob is also admin in alice's space """
    with scopes_disabled():
        alice = User.objects.create_user(username='alice', is_superuser=True)
        bob = User.objects.create_user(username='bob')
        carol = User.objects.create_user(username='carol')
        dave = User.objects.create_user(username='dave')  # user without any space
        sa = create_space_for_user(alice).space
        sb = create_space_for_user(bob).space
        sc = create_space_for_user(carol).space
        UserSpace.objects.create(user=bob, space=sa).groups.add(Group.objects.get(name='admin'))
        Keyword.add_root(name='carols keyword', space=sc)
        return {'alice': alice, 'bob': bob, 'carol': carol, 'dave': dave, 'sa': sa, 'sb': sb, 'sc': sc}


@pytest.fixture
def single_tenant(legacy, settings):
    settings.SINGLE_TENANT_ID = str(legacy['sa'].pk)
    with scopes_disabled():
        enforce_single_tenant()
    return legacy


def test_startup_fails_when_unset_and_lists_spaces(legacy, settings):
    settings.SINGLE_TENANT_ID = None
    with pytest.raises(SingleTenantConfigurationError) as e:
        enforce_single_tenant()
    msg = str(e.value)
    assert 'SINGLE_TENANT_ID is required' in msg
    for s in ('sa', 'sb', 'sc'):
        assert legacy[s].name in msg
        assert str(legacy[s].pk) in msg


@pytest.mark.parametrize('value', ['999999', 'abc'])
def test_startup_fails_for_invalid_id(legacy, settings, value):
    settings.SINGLE_TENANT_ID = value
    with pytest.raises(SingleTenantConfigurationError) as e:
        enforce_single_tenant()
    assert legacy['sb'].name in str(e.value)


def test_run_startup_check_exits(legacy, settings):
    from cookbook.helper.single_tenant import run_startup_check
    settings.SINGLE_TENANT_ID = None
    with pytest.raises(SystemExit):
        run_startup_check()


def test_enforce_moves_everybody(single_tenant):
    st = single_tenant
    with scopes_disabled():
        for name in ('alice', 'bob', 'carol', 'dave'):
            memberships = list(UserSpace.objects.filter(user=st[name]))
            assert len(memberships) == 1, name
            assert memberships[0].space == st['sa']
            assert memberships[0].active

        # existing groups in the shared space are kept, new members get the default group
        assert groups(UserSpace.objects.get(user=st['alice'])) == {'admin'}
        assert groups(UserSpace.objects.get(user=st['bob'])) == {'admin'}
        assert groups(UserSpace.objects.get(user=st['carol'])) == {'user'}
        assert groups(UserSpace.objects.get(user=st['dave'])) == {'user'}

        # the old spaces belong to inactive placeholder users, data is kept
        for s in ('sb', 'sc'):
            space = Space.objects.get(pk=st[s].pk)
            assert space.created_by.username == placeholder_username(space.pk)
            assert not space.created_by.is_active
            assert not space.created_by.has_usable_password()
            assert list(UserSpace.objects.filter(space=space).values_list('user__username', flat=True)) == [placeholder_username(space.pk)]
        assert Space.objects.get(pk=st['sa'].pk).created_by == st['alice']
        assert Keyword.objects.filter(space=st['sc'], name='carols keyword').exists()


def test_enforce_is_idempotent(single_tenant):
    with scopes_disabled():
        before = sorted(UserSpace.objects.values_list('user_id', 'space_id'))
        enforce_single_tenant()
        enforce_single_tenant()
        assert sorted(UserSpace.objects.values_list('user_id', 'space_id')) == before
        assert User.objects.filter(username__startswith=PLACEHOLDER_USERNAME_PREFIX).count() == 2


def test_describe_spaces_hides_placeholders(single_tenant):
    text = describe_spaces()
    assert single_tenant['sa'].name in text


def test_new_user_joins_shared_space(single_tenant):
    """ covers every way a user is created: local/social signup, LDAP, remote user, admin """
    with scopes_disabled():
        user = User.objects.create_user(username='ldap_user')
        memberships = list(UserSpace.objects.filter(user=user))
        assert len(memberships) == 1
        assert memberships[0].space == single_tenant['sa']
        assert memberships[0].active
        assert groups(memberships[0]) == {'user'}
        assert not Space.objects.filter(created_by=user).exists()


def test_middleware_never_creates_space(single_tenant, client):
    with scopes_disabled():
        user = User.objects.create_user(username='late')
        UserSpace.objects.filter(user=user).delete()  # e.g. removed by admin / left the space
        space_count = Space.objects.count()
    client.force_login(user)
    assert client.get(reverse('api:keyword-list')).status_code == 200
    with scopes_disabled():
        assert Space.objects.count() == space_count
        assert list(UserSpace.objects.filter(user=user).values_list('space_id', flat=True)) == [single_tenant['sa'].pk]


def test_create_space_for_user_joins_shared_space(single_tenant):
    with scopes_disabled():
        us = create_space_for_user(single_tenant['carol'])
        assert us.space == single_tenant['sa']
        assert Space.objects.count() == 3


def test_space_creation_api_blocked(single_tenant, client):
    client.force_login(single_tenant['alice'])
    r = client.post(reverse('api:space-list'), {'name': 'another'}, content_type='application/json')
    assert r.status_code == 400
    with scopes_disabled():
        assert Space.objects.count() == 3


def _link(space, group, creator):
    return InviteLink.objects.create(group=Group.objects.get(name=group), valid_until=timezone.now().date() + timedelta(days=7), space=space, created_by=creator)


def test_invite_to_old_space_rejected(single_tenant, client):
    with scopes_disabled():
        link = _link(single_tenant['sb'], 'admin', single_tenant['bob'])
    client.force_login(single_tenant['dave'])
    client.get(reverse('view_invite', args=[str(link.uuid)]))
    with scopes_disabled():
        assert list(UserSpace.objects.filter(user=single_tenant['dave']).values_list('space_id', flat=True)) == [single_tenant['sa'].pk]


def test_invite_to_shared_space_applies_group(single_tenant, client):
    with scopes_disabled():
        link = _link(single_tenant['sa'], 'guest', single_tenant['alice'])
        user = User.objects.create_user(username='invited')  # created by signup, already force joined
    client.force_login(user)
    client.get(reverse('view_invite', args=[str(link.uuid)]))
    with scopes_disabled():
        us = UserSpace.objects.get(user=user)
        assert us.space == single_tenant['sa']
        assert groups(us) == {'guest'}
        link.refresh_from_db()
        assert link.used_by == user


def test_invite_never_downgrades_admin(single_tenant, client):
    with scopes_disabled():
        link = _link(single_tenant['sa'], 'guest', single_tenant['alice'])
    client.force_login(single_tenant['alice'])
    client.get(reverse('view_invite', args=[str(link.uuid)]))
    with scopes_disabled():
        assert groups(UserSpace.objects.get(user=single_tenant['alice'])) == {'admin'}


@override_settings(SOCIAL_DEFAULT_ACCESS=True, SOCIAL_DEFAULT_GROUP='guest')
def test_social_signup_form_uses_shared_space(single_tenant):
    with scopes_disabled():
        user = User.objects.create_user(username='social')
        AllAuthSocialSignupForm.signup(None, RequestFactory().get('/'), user)
        memberships = list(UserSpace.objects.filter(user=user))
        assert len(memberships) == 1
        assert memberships[0].space == single_tenant['sa']
        assert groups(memberships[0]) == {'guest'}


def test_bootstrap_first_user_creates_configured_space(settings):
    settings.SINGLE_TENANT_ID = '42'
    with scopes_disabled():
        assert not Space.objects.exists()
        enforce_single_tenant()  # nothing to do yet, must not fail
        first = User.objects.create_user(username='first', is_superuser=True)
        us = UserSpace.objects.get(user=first)
        assert us.space.pk == 42
        assert us.space.created_by == first
        assert groups(us) == {'admin'}

        second = User.objects.create_user(username='second')
        us2 = UserSpace.objects.get(user=second)
        assert us2.space.pk == 42
        assert groups(us2) == {'user'}
        assert Space.objects.count() == 1
