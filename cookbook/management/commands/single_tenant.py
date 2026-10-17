from django.core.management.base import BaseCommand, CommandError

from cookbook.helper.single_tenant import SingleTenantConfigurationError, describe_spaces, enforce_single_tenant


# python manage.py single_tenant          -> validate SINGLE_TENANT_ID and move all users into that space
# python manage.py single_tenant --list   -> only list existing spaces
class Command(BaseCommand):
    help = 'Validates SINGLE_TENANT_ID and forces all users into that space (runs automatically on startup).'

    def add_arguments(self, parser):
        parser.add_argument('--list', action='store_true', help='only list existing spaces')

    def handle(self, *args, **options):
        if options['list']:
            self.stdout.write(describe_spaces())
            return
        try:
            enforce_single_tenant(stdout=self.stdout)
        except SingleTenantConfigurationError as e:
            raise CommandError(str(e))
