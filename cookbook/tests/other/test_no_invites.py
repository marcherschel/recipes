from datetime import timedelta

import pytest
from django.contrib import auth
from django.contrib.auth.models import Group, User
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from django_scopes import scopes_disabled

from cookbook.helper.AllAuthCustomAdapter import AllAuthCustomAdapter
from cookbook.models import InviteLink, UserSpace

pytestmark = pytest.mark.django_db


@pytest.fixture
def no_invites(settings):
    settings.NO_INVITES = True
    settings.SOCIAL_PROVIDERS = ['allauth.socialaccount.providers.dummy']


@pytest.fixture
def owned_space(space_1, a1_s1):
    with scopes_disabled():
        space_1.created_by = auth.get_user(a1_s1)
        space_1.save()
    return space_1


def _link(space, creator):
    with scopes_disabled():
        return InviteLink.objects.create(group=Group.objects.get(name='admin'), valid_until=timezone.now().date() + timedelta(days=7),
                                         space=space, created_by=creator)


def _request(view_name, session=None):
    request = RequestFactory().get('/')
    request.session = session or {}
    request.resolver_match = type('M', (), {'view_name': view_name})()
    return request


def test_api_create_blocked(no_invites, owned_space, a1_s1):
    r = a1_s1.post(reverse('api:invitelink-list'), {'group': {'id': 1, 'name': 'admin'}}, content_type='application/json')
    assert r.status_code == 403
    with scopes_disabled():
        assert not InviteLink.objects.exists()


def test_api_create_allowed_without_flag(settings, owned_space, a1_s1):
    settings.NO_INVITES = False
    r = a1_s1.post(reverse('api:invitelink-list'), {'group': {'id': 1, 'name': 'admin'}}, content_type='application/json')
    assert r.status_code == 201


def test_api_update_blocked_list_and_delete_allowed(no_invites, owned_space, a1_s1):
    link = _link(owned_space, auth.get_user(a1_s1))
    assert a1_s1.patch(reverse('api:invitelink-detail', args=[link.id]), {'reusable': True}, content_type='application/json').status_code == 403
    assert a1_s1.get(reverse('api:invitelink-list')).status_code == 200
    assert a1_s1.delete(reverse('api:invitelink-detail', args=[link.id])).status_code == 204


def test_existing_link_cannot_be_redeemed(no_invites, owned_space, a1_s1, client):
    link = _link(owned_space, auth.get_user(a1_s1))
    with scopes_disabled():
        outsider = User.objects.create_user(username='outsider')
    client.force_login(outsider)
    client.get(reverse('view_invite', args=[str(link.uuid)]))
    with scopes_disabled():
        assert not UserSpace.objects.filter(user=outsider, space=owned_space).exists()
        link.refresh_from_db()
        assert link.used_by is None


def test_anonymous_invite_does_not_open_signup(no_invites, owned_space, a1_s1, client):
    link = _link(owned_space, auth.get_user(a1_s1))
    r = client.get(reverse('view_invite', args=[str(link.uuid)]))
    assert 'signup_token' not in client.session
    assert reverse('account_signup') not in r.get('Location', '')


def test_local_signup_closed(no_invites, settings, owned_space, a1_s1):
    settings.ENABLE_SIGNUP = True
    link = _link(owned_space, auth.get_user(a1_s1))
    adapter = AllAuthCustomAdapter()
    assert not adapter.is_open_for_signup(_request('account_signup'))
    assert not adapter.is_open_for_signup(_request('account_signup', {'signup_token': str(link.uuid)}))


def test_social_signup_still_open(no_invites):
    assert AllAuthCustomAdapter().is_open_for_signup(_request('socialaccount_signup'))
