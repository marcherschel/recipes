"""
WSGI config for recipes project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/2.0/howto/deployment/wsgi/
"""

import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "recipes.settings")

_application = get_wsgi_application()

# single tenant mode is mandatory: abort startup (listing all spaces) if SINGLE_TENANT_ID is missing/invalid,
# otherwise force every user into the configured space
from cookbook.helper.single_tenant import run_startup_check  # noqa: E402

run_startup_check()


# allows proxy servers to serve application at a subfolder
# NGINX config example is included in nginx/conf.d

def application(environ, start_response):
    # http://flask.pocoo.org/snippets/35/
    script_name = environ.get('HTTP_X_SCRIPT_NAME', '')
    if script_name:
        environ['SCRIPT_NAME'] = script_name
        path_info = environ['PATH_INFO']
        if path_info.startswith(script_name):
            environ['PATH_INFO'] = path_info[len(script_name):]

    scheme = environ.get('HTTP_X_SCHEME', '')
    if scheme:
        environ['wsgi.url_scheme'] = scheme

    return _application(environ, start_response)
