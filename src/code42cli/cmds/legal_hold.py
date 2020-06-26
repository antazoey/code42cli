from collections import OrderedDict
from functools import lru_cache
from pprint import pprint

import click
from click import echo

from py42.exceptions import Py42ForbiddenError, Py42BadRequestError

from code42cli.errors import (
    UserAlreadyAddedError,
    UserNotInLegalHoldError,
    LegalHoldNotFoundOrPermissionDeniedError,
)
from code42cli.util import (
    format_to_table,
    find_format_width,
    format_string_list_to_columns,
)
from code42cli.cmds.shared import get_user_id
from code42cli.file_readers import read_csv_arg
from code42cli.options import global_options, OrderedGroup
from code42cli.bulk import run_bulk_process, generate_template_cmd_factory

_MATTER_KEYS_MAP = OrderedDict()
_MATTER_KEYS_MAP["legalHoldUid"] = "Matter ID"
_MATTER_KEYS_MAP["name"] = "Name"
_MATTER_KEYS_MAP["description"] = "Description"
_MATTER_KEYS_MAP["creator_username"] = "Creator"
_MATTER_KEYS_MAP["creationDate"] = "Creation Date"


@click.group(cls=OrderedGroup)
@global_options
def legal_hold(state):
    """For adding and removing employees to legal hold matters."""
    pass


matter_id_option = click.option(
    "-m",
    "--matter-id",
    required=True,
    type=str,
    help="ID of the legal hold matter user will be added to.",
)
user_id_option = click.option(
    "-u",
    "--username",
    required=True,
    type=str,
    help="The username of the user to add to the matter.",
)


@legal_hold.command()
@matter_id_option
@user_id_option
@global_options
def add_user(state, matter_id, username):
    """Add a user to a legal hold matter."""
    _add_user_to_legal_hold(state.sdk, matter_id, username)


@legal_hold.command()
@matter_id_option
@user_id_option
@global_options
def remove_user(state, matter_id, username):
    """Remove a user from a legal hold matter."""
    _remove_user_from_legal_hold(state.sdk, matter_id, username)


@legal_hold.command("list")
@global_options
def _list(state):
    """Fetch existing legal hold matters."""
    matters = _get_all_active_matters(state.sdk)
    if matters:
        rows, column_size = find_format_width(matters, _MATTER_KEYS_MAP)
        format_to_table(rows, column_size)


@legal_hold.command()
@click.argument("matter-id")
@click.option("--include-inactive", is_flag=True)
@click.option("--include-policy", is_flag=True)
@global_options
def show(state, matter_id, include_inactive=False, include_policy=False):
    """Display details of a given legal hold matter."""
    matter = _check_matter_is_accessible(state.sdk, matter_id)
    matter["creator_username"] = matter["creator"]["username"]

    # if `active` is None then all matters (whether active or inactive) are returned. True returns
    # only those that are active.
    active = None if include_inactive else True
    memberships = _get_legal_hold_memberships_for_matter(state.sdk, matter_id, active=active)
    active_usernames = [member["user"]["username"] for member in memberships if member["active"]]
    inactive_usernames = [
        member["user"]["username"] for member in memberships if not member["active"]
    ]

    rows, column_size = find_format_width([matter], _MATTER_KEYS_MAP)

    echo("")
    format_to_table(rows, column_size)
    if active_usernames:
        echo("\nActive matter members:\n")
        format_string_list_to_columns(active_usernames)
    else:
        echo("\nNo active matter members.\n")

    if include_inactive:
        if inactive_usernames:
            echo("\nInactive matter members:\n")
            format_string_list_to_columns(inactive_usernames)
        else:
            echo("No inactive matter members.\n")

    if include_policy:
        _get_and_print_preservation_policy(state.sdk, matter["holdPolicyUid"])
        echo("")


@legal_hold.group(cls=OrderedGroup)
@global_options
def bulk(state):
    """Tools for executing bulk commands."""
    pass


LEGAL_HOLD_CSV_HEADERS = ["matter_id", "username"]


legal_hold_generate_template = generate_template_cmd_factory(
    csv_columns=LEGAL_HOLD_CSV_HEADERS, cmd_name="legal_hold"
)
bulk.add_command(legal_hold_generate_template)


@bulk.command(
    help="Bulk add users to legal hold matters from a csv file. CSV file format: {}".format(
        ",".join(LEGAL_HOLD_CSV_HEADERS)
    )
)
@read_csv_arg(headers=LEGAL_HOLD_CSV_HEADERS)
@global_options
def add(state, csv_rows):
    row_handler = lambda matter_id, username: _add_user_to_legal_hold(
        state.sdk, matter_id, username
    )
    run_bulk_process(row_handler, csv_rows, progress_label="Adding users to legal hold: ")


@bulk.command(
    help="Bulk remove users from legal hold matters from a csv file. CSV file format: {}".format(
        ",".join(LEGAL_HOLD_CSV_HEADERS)
    )
)
@read_csv_arg(headers=LEGAL_HOLD_CSV_HEADERS)
@global_options
def remove(state, csv_rows):
    row_handler = lambda matter_id, username: _remove_user_from_legal_hold(
        state.sdk, matter_id, username
    )
    run_bulk_process(row_handler, csv_rows, progress_label="Removing users from legal hold:")


def _add_user_to_legal_hold(sdk, matter_id, username):
    user_id = get_user_id(sdk, username)
    matter = _check_matter_is_accessible(sdk, matter_id)
    try:
        sdk.legalhold.add_to_matter(user_id, matter_id)
    except Py42BadRequestError as e:
        if "USER_ALREADY_IN_HOLD" in e.response.text:
            matter_id_and_name_text = "legal hold matter id={}, name={}".format(
                matter_id, matter["name"]
            )
            raise UserAlreadyAddedError(username, matter_id_and_name_text)
        raise


def _remove_user_from_legal_hold(sdk, matter_id, username):
    _check_matter_is_accessible(sdk, matter_id)
    membership_id = _get_legal_hold_membership_id_for_user_and_matter(sdk, username, matter_id)
    sdk.legalhold.remove_from_matter(membership_id)


def _get_and_print_preservation_policy(sdk, policy_uid):
    preservation_policy = sdk.legalhold.get_policy_by_uid(policy_uid)
    print("\nPreservation Policy:\n")
    pprint(preservation_policy._data_root)


def _get_legal_hold_membership_id_for_user_and_matter(sdk, username, matter_id):
    user_id = get_user_id(sdk, username)
    memberships = _get_legal_hold_memberships_for_matter(sdk, matter_id, active=True)
    for member in memberships:
        if member["user"]["userUid"] == user_id:
            return member["legalHoldMembershipUid"]
    raise UserNotInLegalHoldError(username, matter_id)


def _get_legal_hold_memberships_for_matter(sdk, matter_id, active=True):
    memberships_generator = sdk.legalhold.get_all_matter_custodians(
        legal_hold_uid=matter_id, active=active
    )
    memberships = [
        member for page in memberships_generator for member in page["legalHoldMemberships"]
    ]
    return memberships


def _get_all_active_matters(sdk):
    matters_generator = sdk.legalhold.get_all_matters()
    matters = [
        matter for page in matters_generator for matter in page["legalHolds"] if matter["active"]
    ]
    for matter in matters:
        matter["creator_username"] = matter["creator"]["username"]
    return matters


@lru_cache(maxsize=None)
def _check_matter_is_accessible(sdk, matter_id):
    try:
        matter = sdk.legalhold.get_matter_by_uid(matter_id)
        return matter
    except (Py42BadRequestError, Py42ForbiddenError):
        raise LegalHoldNotFoundOrPermissionDeniedError(matter_id)
