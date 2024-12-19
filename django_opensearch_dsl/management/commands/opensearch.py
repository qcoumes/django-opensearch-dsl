import argparse
import functools
import operator
import sys
from argparse import ArgumentParser
from collections import defaultdict
from typing import Any, Callable

from django.core.exceptions import FieldError
from django.core.management import BaseCommand
from django.db.models import Q

from django_opensearch_dsl.registries import registry

from ...utils import manage_index
from ..enums import OpensearchAction
from ..types import parse


class Command(BaseCommand):
    """Manage indices and documents."""

    help = (
        "Allow to create and delete indices, as well as indexing, updating or deleting specific "
        "documents from specific indices.\n"
    )

    def __init__(self, *args, **kwargs):  # noqa
        super(Command, self).__init__()
        self.usage = None

    def db_filter(self, parser: ArgumentParser) -> Callable[[str], Any]:
        """Return a function to parse the filters."""

        def wrap(value):  # pragma: no cover
            try:
                lookup, v = value.split("=")
                v = parse(v)
            except ValueError:
                sys.stderr.write(parser._subparsers._group_actions[0].choices["document"].format_usage())  # noqa
                sys.stderr.write(
                    f"manage.py index: error: invalid filter: '{value}' (filter must be formatted as "
                    f"'[Field Lookups]=[value]')\n",
                )
                exit(1)
            return lookup, v  # noqa

        return wrap

    def __list_index(self, **options):  # noqa pragma: no cover
        """List all known index and indicate whether they are created or not."""
        indices = registry.get_indices()
        result = defaultdict(list)
        for index in indices:
            module = index._doc_types[0].__module__.split(".")[-2]  # noqa
            exists = index.exists()
            checkbox = f"[{'X' if exists else ' '}]"
            count = f" ({index.search().count()} documents)" if exists else ""
            result[module].append(f"{checkbox} {index._name}{count}")
        for app, indices in result.items():
            self.stdout.write(self.style.MIGRATE_LABEL(app))
            self.stdout.write("\n".join(indices))

    def _manage_index(self, action, indices, force, verbosity, ignore_error, **options):  # noqa
        """Manage the creation and deletion of indices."""
        manage_index(
            action, indices, force, ignore_error, verbosity, stderr=self.stderr, stdout=self.stdout, style=self.style
        )

    def _manage_document(
        self,
        action,
        indices,
        objects,
        force,
        filters,
        excludes,
        verbosity,
        parallel,
        count,
        refresh,
        missing,
        database,
        batch_size,
        batch_type,
        **options,
    ):  # noqa
        """Manage the creation and deletion of indices."""
        action = OpensearchAction(action)
        known = registry.get_indices()
        filter_ = functools.reduce(operator.and_, (Q(**{k: v}) for k, v in filters)) if filters else None
        exclude = functools.reduce(operator.and_, (Q(**{k: v}) for k, v in excludes)) if excludes else None

        # Filter existing objects
        valid_models = []
        registered_models = [m.__name__.lower() for m in registry.get_models()]
        if objects:
            for model in objects:
                if model.lower() in registered_models:
                    valid_models.append(model)
                else:
                    self.stderr.write(f"Unknown object '{model}', choices are: '{registered_models}'")
                    exit(1)

        # Filter indices
        if indices:
            # Ensure every given indices exists
            known_name = [i._name for i in known]  # noqa
            unknown = set(indices) - set(known_name)
            if unknown:
                self.stderr.write(f"Unknown indices '{list(unknown)}', choices are: '{known_name}'")
                exit(1)

            # Only keep given indices
            indices = list(filter(lambda i: i._name in indices, known))  # noqa
        else:
            indices = known

        # Ensure every indices needed are created
        not_created = [i._name for i in indices if not i.exists()]  # noqa
        if not_created:
            self.stderr.write(f"The following indices are not created : {not_created}")
            self.stderr.write("Use 'python3 manage.py opensearch list' to list indices' state.")
            exit(1)

        # Check field, preparing to display expected actions
        s = f"The following documents will be {action.past}:"
        kwargs_list = []

        if objects:
            django_models = [m for m in registry.get_models() if m.__name__.lower() in valid_models]
            all_os_models = []
            selected_os_models = []
            indices_raw = registry.get_indices_raw()

            for k, v in indices_raw.items():
                for model in list(v):
                    all_os_models.append(model)

            for os_model in all_os_models:
                if os_model.django.model in django_models and os_model.Index.name in list(i._name for i in indices):
                    selected_os_models.append(os_model)

            # Handle --missing
            exclude_ = exclude
            for model in selected_os_models:
                try:
                    kwargs_list.append({"filter_": filter_, "exclude": exclude_, "count": count})
                    qs = model().get_queryset(filter_=filter_, exclude=exclude_, count=count).count()
                except FieldError as e:
                    self.stderr.write(f"Error while filtering on '{model.django.model.__name__}':\n{e}'")  # noqa
                    exit(1)
                else:
                    s += f"\n\t- {qs} {model.django.model.__name__}."
        else:
            for index in indices:
                # Handle --missing
                exclude_ = exclude
                if missing and action == OpensearchAction.INDEX:
                    q = Q(pk__in=[h.meta.id for h in index.search().extra(stored_fields=[]).scan()])
                    exclude_ = exclude_ & q if exclude_ is not None else q

                document = index._doc_types[0]()  # noqa
                try:
                    kwargs_list.append({"db_alias": database, "filter_": filter_, "exclude": exclude_, "count": count})
                    qs = document.get_queryset(filter_=filter_, exclude=exclude_, count=count).count()
                except FieldError as e:
                    model = index._doc_types[0].django.model.__name__  # noqa
                    self.stderr.write(f"Error while filtering on '{model}' (from index '{index._name}'):\n{e}'")  # noqa
                    exit(1)
                else:
                    s += f"\n\t- {qs} {document.django.model.__name__}."

        # Display expected actions
        if verbosity or not force:
            self.stdout.write(s + "\n\n")

        # Ask for confirmation to continue
        if not force:  # pragma: no cover
            while True:
                p = input("Continue ? [y]es [n]o : ")
                if p.lower() in ["yes", "y"]:
                    self.stdout.write("\n")
                    break
                elif p.lower() in ["no", "n"]:
                    exit(1)

        result = "\n"
        if objects:
            for model, kwargs in zip(selected_os_models, kwargs_list):
                document = model()  # noqa
                qs = document.get_indexing_queryset(
                    stdout=self.stdout._out,
                    verbose=verbosity,
                    action=action,
                    batch_size=batch_size,
                    batch_type=batch_type,
                    **kwargs,
                )
                success, errors = document.update(
                    qs, parallel=parallel, refresh=refresh, action=action, raise_on_error=False
                )

                success_str = self.style.SUCCESS(success) if success else success
                errors_str = self.style.ERROR(len(errors)) if errors else len(errors)
                model = document.django.model.__name__

                if verbosity == 1:
                    result += f"{success_str} {model} successfully {action.past}, {errors_str} errors:\n"
                    reasons = defaultdict(int)
                    for e in errors:  # Count occurrence of each error
                        error = e.get(action, {"result": "unknown error"}).get("result", "unknown error")
                        reasons[error] += 1
                    for reasons, total in reasons.items():
                        result += f"    - {reasons} : {total}\n"

                if verbosity > 1:
                    result += f"{success_str} {model} successfully {action}d, {errors_str} errors:\n {errors}\n"

        else:
            for index, kwargs in zip(indices, kwargs_list):
                document = index._doc_types[0]()  # noqa
                qs = document.get_indexing_queryset(
                    stdout=self.stdout._out,
                    verbose=verbosity,
                    action=action,
                    batch_size=batch_size,
                    batch_type=batch_type,
                    **kwargs,
                )
                success, errors = document.update(
                    qs, parallel=parallel, refresh=refresh, action=action, raise_on_error=False
                )

                success_str = self.style.SUCCESS(success) if success else success
                errors_str = self.style.ERROR(len(errors)) if errors else len(errors)
                model = document.django.model.__name__

                if verbosity == 1:
                    result += f"{success_str} {model} successfully {action.past}, {errors_str} errors:\n"
                    reasons = defaultdict(int)
                    for e in errors:  # Count occurrence of each error
                        error = e.get(action, {"result": "unknown error"}).get("result", "unknown error")
                        reasons[error] += 1
                    for reasons, total in reasons.items():
                        result += f"    - {reasons} : {total}\n"

                if verbosity > 1:
                    result += f"{success_str} {model} successfully {action}d, {errors_str} errors:\n {errors}\n"

        if verbosity:
            self.stdout.write(result + "\n")

    def add_arguments(self, parser):
        """Add arguments to parser."""
        parser.formatter_class = argparse.RawTextHelpFormatter
        subparsers = parser.add_subparsers()

        # 'list' subcommand
        subparser = subparsers.add_parser(
            "list",
            help="Show all available indices (and their state) for the current project.",
            description="Show all available indices (and their state) for the current project.",
        )
        subparser.set_defaults(func=self.__list_index)

        # 'index' subcommand
        subparser = subparsers.add_parser(
            "index",
            help="Manage the creation an deletion of indices.",
            description="Manage the creation an deletion of indices.",
        )
        subparser.set_defaults(func=self._manage_index)
        subparser.add_argument(
            "action",
            type=str,
            help=(
                "Whether you want to create, update, delete or rebuild the indices.\n"
                "Update allow you to update your indices mappings if you modified them after creation. "
                "This should be done prior to indexing new document with dynamic mapping (enabled by default), "
                "a default mapping with probably the wrong type would be created for any new field."
            ),
            choices=[
                OpensearchAction.CREATE.value,
                OpensearchAction.DELETE.value,
                OpensearchAction.REBUILD.value,
                OpensearchAction.UPDATE.value,
            ],
        )
        subparser.add_argument("--force", action="store_true", default=False, help="Do not ask for confirmation.")
        subparser.add_argument("--ignore-error", action="store_true", default=False, help="Do not stop on error.")
        subparser.add_argument(
            "indices",
            type=str,
            nargs="*",
            metavar="INDEX",
            help="Only manage the given indices.",
        )

        # 'document' subcommand
        subparser = subparsers.add_parser(
            "document",
            help="Manage the indexation and creation of documents.",
            description="Manage the indexation and creation of documents.",
            formatter_class=argparse.RawTextHelpFormatter,
        )
        subparser.set_defaults(func=self._manage_document)
        subparser.add_argument(
            "action",
            type=str,
            help="Whether you want to create, delete or rebuild the indices.",
            choices=[
                OpensearchAction.INDEX.value,
                OpensearchAction.DELETE.value,
                OpensearchAction.UPDATE.value,
            ],
        )
        subparser.add_argument(
            "-d",
            "--database",
            type=str,
            default=None,
            help="Nominates a database to use as source.",
        )
        subparser.add_argument(
            "-f",
            "--filters",
            type=self.db_filter(parser),
            nargs="*",
            help=(
                "Filter object in the queryset. Argument must be formatted as '[lookup]=[value]', e.g. "
                "'document_date__gte=2020-05-21.\n"
                "The accepted value type are:\n"
                "  - 'None' ('[lookup]=')\n"
                "  - 'float' ('[lookup]=1.12')\n"
                "  - 'int' ('[lookup]=23')\n"
                "  - 'datetime.date' ('[lookup]=2020-10-08')\n"
                "  - 'list' ('[lookup]=1,2,3,4') Value between comma ',' can be of any other accepted value type\n"
                "  - 'str' ('[lookup]=week') Value that didn't match any type above will be interpreted as a str\n"
                "The list of lookup function can be found here: "
                "https://docs.djangoproject.com/en/dev/ref/models/querysets/#field-lookups"
            ),
        )
        subparser.add_argument(
            "-e",
            "--excludes",
            type=self.db_filter(parser),
            nargs="*",
            help=(
                "Exclude objects from the queryset. Argument must be formatted as '[lookup]=[value]', see '--filters' "
                "for more information"
            ),
        )
        subparser.add_argument("--force", action="store_true", default=False, help="Do not ask for confirmation.")
        subparser.add_argument(
            "-i", "--indices", type=str, nargs="*", help="Only update documents on the given indices."
        )
        subparser.add_argument("-o", "--objects", type=str, nargs="*", help="Only update selected objects.")
        subparser.add_argument(
            "-c", "--count", type=int, default=None, help="Update at most COUNT objects (0 to index everything)."
        )
        subparser.add_argument(
            "-p",
            "--parallel",
            action="store_true",
            default=False,
            help="Parallelize the communication with Opensearch.",
        )
        subparser.add_argument(
            "-r",
            "--refresh",
            action="store_true",
            default=False,
            help="Make operations performed on the indices immediatly available for search.",
        )
        subparser.add_argument(
            "-m",
            "--missing",
            action="store_true",
            default=False,
            help="When used with 'index' action, only index documents not indexed yet.",
        )
        subparser.add_argument(
            "-b",
            "--batch-size",
            type=int,
            default=None,
            help="Specify the batch size for processing documents.",
        )
        subparser.add_argument(
            "-t",
            "--batch-type",
            type=str,
            default="offset",
            help="Specify the batch type for processing documents (pk_filters | offset).",
        )

        self.usage = parser.format_usage()

    def handle(self, *args, **options):
        """Run the command according to `options`."""
        if "func" not in options:  # pragma: no cover
            self.stderr.write(self.usage)
            self.stderr.write(f"manage.py opensearch: error: no subcommand provided.")
            exit(1)

        options["func"](**options)
