# -*- coding: utf-8 -*-
# Copyright 2017 Vector Creations Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from twisted.internet import defer

from synapse.api.errors import SynapseError

from ._base import SQLBaseStore

import ujson as json


# The category ID for the "default" category. We don't store as null in the
# database to avoid the fun of null != null
_DEFAULT_CATEGORY_ID = ""
_DEFAULT_ROLE_ID = ""


class GroupServerStore(SQLBaseStore):
    def get_group(self, group_id):
        return self._simple_select_one(
            table="groups",
            keyvalues={
                "group_id": group_id,
            },
            retcols=("name", "short_description", "long_description", "avatar_url",),
            allow_none=True,
            desc="is_user_in_group",
        )

    def get_users_in_group(self, group_id, include_private=False):
        # TODO: Pagination

        keyvalues = {
            "group_id": group_id,
        }
        if not include_private:
            keyvalues["is_public"] = True

        return self._simple_select_list(
            table="group_users",
            keyvalues=keyvalues,
            retcols=("user_id", "is_public",),
            desc="get_users_in_group",
        )

    def get_rooms_in_group(self, group_id, include_private=False):
        # TODO: Pagination

        keyvalues = {
            "group_id": group_id,
        }
        if not include_private:
            keyvalues["is_public"] = True

        return self._simple_select_list(
            table="group_rooms",
            keyvalues=keyvalues,
            retcols=("room_id", "is_public",),
            desc="get_rooms_in_group",
        )

    def get_rooms_for_summary_by_category(self, group_id, include_private=False):
        """Get the rooms and categories that should be included in a summary request

        Returns ([rooms], [categories])
        """
        def _get_rooms_for_summary_txn(txn):
            keyvalues = {
                "group_id": group_id,
            }
            if not include_private:
                keyvalues["is_public"] = True

            sql = """
                SELECT room_id, is_public, category_id, room_order
                FROM group_summary_rooms
                WHERE group_id = ?
            """

            if not include_private:
                sql += " AND is_public = ?"
                txn.execute(sql, (group_id, True))
            else:
                txn.execute(sql, (group_id,))

            rooms = [
                {
                    "room_id": row[0],
                    "is_public": row[1],
                    "category_id": row[2] if row[2] != _DEFAULT_CATEGORY_ID else None,
                    "order": row[3],
                }
                for row in txn
            ]

            sql = """
                SELECT category_id, is_public, profile, cat_order
                FROM group_summary_room_categories
                INNER JOIN group_room_categories USING (group_id, category_id)
                WHERE group_id = ?
            """

            if not include_private:
                sql += " AND is_public = ?"
                txn.execute(sql, (group_id, True))
            else:
                txn.execute(sql, (group_id,))

            categories = {
                row[0]: {
                    "is_public": row[1],
                    "profile": json.loads(row[2]),
                    "order": row[3],
                }
                for row in txn
            }

            return rooms, categories
        return self.runInteraction(
            "get_rooms_for_summary", _get_rooms_for_summary_txn
        )

    def add_room_to_summary(self, group_id, room_id, category_id, order, is_public):
        return self.runInteraction(
            "add_room_to_summary", self._add_room_to_summary_txn,
            group_id, room_id, category_id, order, is_public,
        )

    def _add_room_to_summary_txn(self, txn, group_id, room_id, category_id, order,
                                 is_public):
        """Add (or update) room's entry in summary.

        Args:
            group_id (str)
            room_id (str)
            category_id (str): If not None then adds the category to the end of
                the summary if its not already there. [Optional]
            order (int): If not None inserts the room at that position, e.g.
                an order of 1 will put the room first. Otherwise, the room gets
                added to the end.
        """

        if category_id is None:
            category_id = _DEFAULT_CATEGORY_ID
        else:
            cat_exists = self._simple_select_one_onecol_txn(
                txn,
                table="group_room_categories",
                keyvalues={
                    "group_id": group_id,
                    "category_id": category_id,
                },
                retcol="group_id",
                allow_none=True,
            )
            if not cat_exists:
                raise SynapseError(400, "Category doesn't exist")

            # TODO: Check category is part of summary already
            cat_exists = self._simple_select_one_onecol_txn(
                txn,
                table="group_summary_room_categories",
                keyvalues={
                    "group_id": group_id,
                    "category_id": category_id,
                },
                retcol="group_id",
                allow_none=True,
            )
            if not cat_exists:
                # If not, add it with an order larger than all others
                txn.execute("""
                    INSERT INTO group_summary_room_categories
                    (group_id, category_id, cat_order)
                    SELECT ?, ?, COALESCE(MAX(cat_order), 0) + 1
                    FROM group_summary_room_categories
                    WHERE group_id = ? AND category_id = ?
                """, (group_id, category_id, group_id, category_id))

        existing = self._simple_select_one_txn(
            txn,
            table="group_summary_rooms",
            keyvalues={
                "group_id": group_id,
                "room_id": room_id,
                "category_id": category_id,
            },
            retcols=("room_order", "is_public",),
            allow_none=True,
        )

        if order is not None:
            # Shuffle other room orders that come after the given order
            sql = """
                UPDATE group_summary_rooms SET room_order = room_order + 1
                WHERE group_id = ? AND category_id = ? AND room_order >= ?
            """
            txn.execute(sql, (group_id, category_id, order,))
        elif not existing:
            sql = """
                SELECT COALESCE(MAX(room_order), 0) + 1 FROM group_summary_rooms
                WHERE group_id = ? AND category_id = ?
            """
            txn.execute(sql, (group_id, category_id,))
            order, = txn.fetchone()

        if existing:
            to_update = {}
            if order is not None:
                to_update["room_order"] = order
            if is_public is not None:
                to_update["is_public"] = is_public
            self._simple_update_txn(
                txn,
                table="group_summary_rooms",
                keyvalues={
                    "group_id": group_id,
                    "category_id": category_id,
                    "room_id": room_id,
                },
                values=to_update,
            )
        else:
            if is_public is None:
                is_public = True

            self._simple_insert_txn(
                txn,
                table="group_summary_rooms",
                values={
                    "group_id": group_id,
                    "category_id": category_id,
                    "room_id": room_id,
                    "room_order": order,
                    "is_public": is_public,
                },
            )

    def remove_room_from_summary(self, group_id, room_id, category_id):
        if category_id is None:
            category_id = _DEFAULT_CATEGORY_ID

        return self._simple_delete(
            table="group_summary_rooms",
            keyvalues={
                "group_id": group_id,
                "category_id": category_id,
                "room_id": room_id,
            },
            desc="remove_room_from_summary",
        )

    @defer.inlineCallbacks
    def get_group_categories(self, group_id):
        rows = yield self._simple_select_list(
            table="group_room_categories",
            keyvalues={
                "group_id": group_id,
            },
            retcols=("category_id", "is_public", "profile"),
            desc="get_group_categories",
        )

        defer.returnValue({
            row["category_id"]: {
                "is_public": row["is_public"],
                "profile": json.loads(row["profile"]),
            }
            for row in rows
        })

    @defer.inlineCallbacks
    def get_group_category(self, group_id, category_id):
        category = yield self._simple_select_one(
            table="group_room_categories",
            keyvalues={
                "group_id": group_id,
                "category_id": category_id,
            },
            retcols=("is_public", "profile"),
            desc="get_group_category",
        )

        category["profile"] = json.loads(category["profile"])

        defer.returnValue(category)

    def upsert_group_category(self, group_id, category_id, profile, is_public):
        """Add/update room category for group
        """
        insertion_values = {}
        update_values = {"category_id": category_id}  # This cannot be empty

        if profile is None:
            insertion_values["profile"] = "{}"
        else:
            update_values["profile"] = json.dumps(profile)

        if is_public is None:
            insertion_values["is_public"] = True
        else:
            update_values["is_public"] = is_public

        return self._simple_upsert(
            table="group_room_categories",
            keyvalues={
                "group_id": group_id,
                "category_id": category_id,
            },
            values=update_values,
            insertion_values=insertion_values,
            desc="upsert_group_category",
        )

    def remove_group_category(self, group_id, category_id):
        return self._simple_delete(
            table="group_room_categories",
            keyvalues={
                "group_id": group_id,
                "category_id": category_id,
            },
            desc="remove_group_category",
        )

    @defer.inlineCallbacks
    def get_group_roles(self, group_id):
        rows = yield self._simple_select_list(
            table="group_roles",
            keyvalues={
                "group_id": group_id,
            },
            retcols=("role_id", "is_public", "profile"),
            desc="get_group_roles",
        )

        defer.returnValue({
            row["role_id"]: {
                "is_public": row["is_public"],
                "profile": json.loads(row["profile"]),
            }
            for row in rows
        })

    @defer.inlineCallbacks
    def get_group_role(self, group_id, role_id):
        role = yield self._simple_select_one(
            table="group_roles",
            keyvalues={
                "group_id": group_id,
                "role_id": role_id,
            },
            retcols=("is_public", "profile"),
            desc="get_group_role",
        )

        role["profile"] = json.loads(role["profile"])

        defer.returnValue(role)

    def upsert_group_role(self, group_id, role_id, profile, is_public):
        """Add/remove user role
        """
        insertion_values = {}
        update_values = {"role_id": role_id}  # This cannot be empty

        if profile is None:
            insertion_values["profile"] = "{}"
        else:
            update_values["profile"] = json.dumps(profile)

        if is_public is None:
            insertion_values["is_public"] = True
        else:
            update_values["is_public"] = is_public

        return self._simple_upsert(
            table="group_roles",
            keyvalues={
                "group_id": group_id,
                "role_id": role_id,
            },
            values=update_values,
            insertion_values=insertion_values,
            desc="upsert_group_role",
        )

    def remove_group_role(self, group_id, role_id):
        return self._simple_delete(
            table="group_roles",
            keyvalues={
                "group_id": group_id,
                "role_id": role_id,
            },
            desc="remove_group_role",
        )

    def add_user_to_summary(self, group_id, user_id, role_id, order, is_public):
        return self.runInteraction(
            "add_user_to_summary", self._add_user_to_summary_txn,
            group_id, user_id, role_id, order, is_public,
        )

    def _add_user_to_summary_txn(self, txn, group_id, user_id, role_id, order,
                                 is_public):
        """Add (or update) user's entry in summary.

        Args:
            group_id (str)
            user_id (str)
            role_id (str): If not None then adds the role to the end of
                the summary if its not already there. [Optional]
            order (int): If not None inserts the user at that position, e.g.
                an order of 1 will put the user first. Otherwise, the user gets
                added to the end.
        """
        if role_id is None:
            role_id = _DEFAULT_ROLE_ID
        else:
            role_exists = self._simple_select_one_onecol_txn(
                txn,
                table="group_roles",
                keyvalues={
                    "group_id": group_id,
                    "role_id": role_id,
                },
                retcol="group_id",
                allow_none=True,
            )
            if not role_exists:
                raise SynapseError(400, "Role doesn't exist")

            # TODO: Check role is part of the summary already
            role_exists = self._simple_select_one_onecol_txn(
                txn,
                table="group_summary_roles",
                keyvalues={
                    "group_id": group_id,
                    "role_id": role_id,
                },
                retcol="group_id",
                allow_none=True,
            )
            if not role_exists:
                # If not, add it with an order larger than all others
                txn.execute("""
                    INSERT INTO group_summary_roles
                    (group_id, role_id, role_order)
                    SELECT ?, ?, COALESCE(MAX(role_order), 0) + 1
                    FROM group_summary_roles
                    WHERE group_id = ? AND role_id = ?
                """, (group_id, role_id, group_id, role_id))

        existing = self._simple_select_one_txn(
            txn,
            table="group_summary_users",
            keyvalues={
                "group_id": group_id,
                "user_id": user_id,
                "role_id": role_id,
            },
            retcols=("user_order", "is_public",),
            allow_none=True,
        )

        if order is not None:
            # Shuffle other users orders that come after the given order
            sql = """
                UPDATE group_summary_users SET user_order = user_order + 1
                WHERE group_id = ? AND role_id = ? AND user_order >= ?
            """
            txn.execute(sql, (group_id, role_id, order,))
        elif not existing:
            sql = """
                SELECT COALESCE(MAX(user_order), 0) + 1 FROM group_summary_users
                WHERE group_id = ? AND role_id = ?
            """
            txn.execute(sql, (group_id, role_id,))
            order, = txn.fetchone()

        if existing:
            to_update = {}
            if order is not None:
                to_update["user_order"] = order
            if is_public is not None:
                to_update["is_public"] = is_public
            self._simple_update_txn(
                txn,
                table="group_summary_users",
                keyvalues={
                    "group_id": group_id,
                    "role_id": role_id,
                    "user_id": user_id,
                },
                values=to_update,
            )
        else:
            if is_public is None:
                is_public = True

            self._simple_insert_txn(
                txn,
                table="group_summary_users",
                values={
                    "group_id": group_id,
                    "role_id": role_id,
                    "user_id": user_id,
                    "user_order": order,
                    "is_public": is_public,
                },
            )

    def remove_user_from_summary(self, group_id, user_id, role_id):
        if role_id is None:
            role_id = _DEFAULT_ROLE_ID

        return self._simple_delete(
            table="group_summary_users",
            keyvalues={
                "group_id": group_id,
                "role_id": role_id,
                "user_id": user_id,
            },
            desc="remove_user_from_summary",
        )

    def get_users_for_summary_by_role(self, group_id, include_private=False):
        """Get the users and roles that should be included in a summary request

        Returns ([users], [roles])
        """
        def _get_users_for_summary_txn(txn):
            keyvalues = {
                "group_id": group_id,
            }
            if not include_private:
                keyvalues["is_public"] = True

            sql = """
                SELECT user_id, is_public, role_id, user_order
                FROM group_summary_users
                WHERE group_id = ?
            """

            if not include_private:
                sql += " AND is_public = ?"
                txn.execute(sql, (group_id, True))
            else:
                txn.execute(sql, (group_id,))

            users = [
                {
                    "user_id": row[0],
                    "is_public": row[1],
                    "role_id": row[2] if row[2] != _DEFAULT_ROLE_ID else None,
                    "order": row[3],
                }
                for row in txn
            ]

            sql = """
                SELECT role_id, is_public, profile, role_order
                FROM group_summary_roles
                INNER JOIN group_roles USING (group_id, role_id)
                WHERE group_id = ?
            """

            if not include_private:
                sql += " AND is_public = ?"
                txn.execute(sql, (group_id, True))
            else:
                txn.execute(sql, (group_id,))

            roles = {
                row[0]: {
                    "is_public": row[1],
                    "profile": json.loads(row[2]),
                    "order": row[3],
                }
                for row in txn
            }

            return users, roles
        return self.runInteraction(
            "get_users_for_summary_by_role", _get_users_for_summary_txn
        )

    def is_user_in_group(self, user_id, group_id):
        return self._simple_select_one_onecol(
            table="group_users",
            keyvalues={
                "group_id": group_id,
                "user_id": user_id,
            },
            retcol="user_id",
            allow_none=True,
            desc="is_user_in_group",
        ).addCallback(lambda r: bool(r))

    def is_user_admin_in_group(self, group_id, user_id):
        return self._simple_select_one_onecol(
            table="group_users",
            keyvalues={
                "group_id": group_id,
                "user_id": user_id,
            },
            retcol="is_admin",
            allow_none=True,
            desc="is_user_adim_in_group",
        )

    def add_group_invite(self, group_id, user_id):
        """Record that the group server has invited a user
        """
        return self._simple_insert(
            table="group_invites",
            values={
                "group_id": group_id,
                "user_id": user_id,
            },
            desc="add_group_invite",
        )

    def is_user_invited_to_local_group(self, group_id, user_id):
        """Has the group server invited a user?
        """
        return self._simple_select_one_onecol(
            table="group_invites",
            keyvalues={
                "group_id": group_id,
                "user_id": user_id,
            },
            retcol="user_id",
            desc="is_user_invited_to_local_group",
            allow_none=True,
        )

    def add_user_to_group(self, group_id, user_id, is_admin=False, is_public=True,
                          local_attestation=None, remote_attestation=None):
        """Add a user to the group server.

        Args:
            group_id (str)
            user_id (str)
            is_admin (bool)
            is_public (bool)
            local_attestation (dict): The attestation the GS created to give
                to the remote server. Optional if the user and group are on the
                same server
            remote_attestation (dict): The attestation given to GS by remote
                server. Optional if the user and group are on the same server
        """
        def _add_user_to_group_txn(txn):
            self._simple_insert_txn(
                txn,
                table="group_users",
                values={
                    "group_id": group_id,
                    "user_id": user_id,
                    "is_admin": is_admin,
                    "is_public": is_public,
                },
            )

            self._simple_delete_txn(
                txn,
                table="group_invites",
                keyvalues={
                    "group_id": group_id,
                    "user_id": user_id,
                },
            )

            if local_attestation:
                self._simple_insert_txn(
                    txn,
                    table="group_attestations_renewals",
                    values={
                        "group_id": group_id,
                        "user_id": user_id,
                        "valid_until_ms": local_attestation["valid_until_ms"],
                    },
                )
            if remote_attestation:
                self._simple_insert_txn(
                    txn,
                    table="group_attestations_remote",
                    values={
                        "group_id": group_id,
                        "user_id": user_id,
                        "valid_until_ms": remote_attestation["valid_until_ms"],
                        "attestation_json": json.dumps(remote_attestation),
                    },
                )

        return self.runInteraction(
            "add_user_to_group", _add_user_to_group_txn
        )

    def remove_user_from_group(self, group_id, user_id):
        def _remove_user_from_group_txn(txn):
            self._simple_delete_txn(
                txn,
                table="group_users",
                keyvalues={
                    "group_id": group_id,
                    "user_id": user_id,
                },
            )
            self._simple_delete_txn(
                txn,
                table="group_invites",
                keyvalues={
                    "group_id": group_id,
                    "user_id": user_id,
                },
            )
            self._simple_delete_txn(
                txn,
                table="group_attestations_renewals",
                keyvalues={
                    "group_id": group_id,
                    "user_id": user_id,
                },
            )
            self._simple_delete_txn(
                txn,
                table="group_attestations_remote",
                keyvalues={
                    "group_id": group_id,
                    "user_id": user_id,
                },
            )
        return self.runInteraction("remove_user_from_group", _remove_user_from_group_txn)

    def add_room_to_group(self, group_id, room_id, is_public):
        return self._simple_insert(
            table="group_rooms",
            values={
                "group_id": group_id,
                "room_id": room_id,
                "is_public": is_public,
            },
            desc="add_room_to_group",
        )

    @defer.inlineCallbacks
    def register_user_group_membership(self, group_id, user_id, membership,
                                       is_admin=False, content={},
                                       local_attestation=None,
                                       remote_attestation=None,
                                       ):
        """Registers that a local user is a member of a (local or remote) group.

        Args:
            group_id (str)
            user_id (str)
            membership (str)
            is_admin (bool)
            content (dict): Content of the membership, e.g. includes the inviter
                if the user has been invited.
            local_attestation (dict): If remote group then store the fact that we
                have given out an attestation, else None.
            remote_attestation (dict): If remote group then store the remote
                attestation from the group, else None.
        """
        def _register_user_group_membership_txn(txn, next_id):
            # TODO: Upsert?
            self._simple_delete_txn(
                txn,
                table="local_group_membership",
                keyvalues={
                    "group_id": group_id,
                    "user_id": user_id,
                },
            )
            self._simple_insert_txn(
                txn,
                table="local_group_membership",
                values={
                    "group_id": group_id,
                    "user_id": user_id,
                    "is_admin": is_admin,
                    "membership": membership,
                    "content": json.dumps(content),
                },
            )
            self._group_updates_stream_cache.entity_has_changed(user_id, next_id)

            # TODO: Insert profile to ensuer it comes down stream if its a join.

            if membership == "join":
                if local_attestation:
                    self._simple_insert_txn(
                        txn,
                        table="group_attestations_renewals",
                        values={
                            "group_id": group_id,
                            "user_id": user_id,
                            "valid_until_ms": local_attestation["valid_until_ms"],
                        }
                    )
                if remote_attestation:
                    self._simple_insert_txn(
                        txn,
                        table="group_attestations_remote",
                        values={
                            "group_id": group_id,
                            "user_id": user_id,
                            "valid_until_ms": remote_attestation["valid_until_ms"],
                            "attestation": json.dumps(remote_attestation),
                        }
                    )
            else:
                self._simple_delete_txn(
                    txn,
                    table="group_attestations_renewals",
                    keyvalues={
                        "group_id": group_id,
                        "user_id": user_id,
                    },
                )
                self._simple_delete_txn(
                    txn,
                    table="group_attestations_remote",
                    keyvalues={
                        "group_id": group_id,
                        "user_id": user_id,
                    },
                )

        with self._group_updates_id_gen.get_next() as next_id:
            yield self.runInteraction(
                "register_user_group_membership",
                _register_user_group_membership_txn, next_id,
            )

    @defer.inlineCallbacks
    def create_group(self, group_id, user_id, name, avatar_url, short_description,
                     long_description,):
        yield self._simple_insert(
            table="groups",
            values={
                "group_id": group_id,
                "name": name,
                "avatar_url": avatar_url,
                "short_description": short_description,
                "long_description": long_description,
            },
            desc="create_group",
        )

    def get_attestations_need_renewals(self, valid_until_ms):
        """Get all attestations that need to be renewed until givent time
        """
        def _get_attestations_need_renewals_txn(txn):
            sql = """
                SELECT group_id, user_id FROM group_attestations_renewals
                WHERE valid_until_ms <= ?
            """
            txn.execute(sql, (valid_until_ms,))
            return self.cursor_to_dict(txn)
        return self.runInteraction(
            "get_attestations_need_renewals", _get_attestations_need_renewals_txn
        )

    def update_attestation_renewal(self, group_id, user_id, attestation):
        """Update an attestation that we have renewed
        """
        return self._simple_update_one(
            table="group_attestations_renewals",
            keyvalues={
                "group_id": group_id,
                "user_id": user_id,
            },
            updatevalues={
                "valid_until_ms": attestation["valid_until_ms"],
            },
            desc="update_attestation_renewal",
        )

    def update_remote_attestion(self, group_id, user_id, attestation):
        """Update an attestation that a remote has renewed
        """
        return self._simple_update_one(
            table="group_attestations_remote",
            keyvalues={
                "group_id": group_id,
                "user_id": user_id,
            },
            updatevalues={
                "valid_until_ms": attestation["valid_until_ms"],
                "attestation_json": json.dumps(attestation)
            },
            desc="update_remote_attestion",
        )

    @defer.inlineCallbacks
    def get_remote_attestation(self, group_id, user_id):
        """Get the attestation that proves the remote agrees that the user is
        in the group.
        """
        row = yield self._simple_select_one(
            table="group_attestations_remote",
            keyvalues={
                "group_id": group_id,
                "user_id": user_id,
            },
            retcols=("valid_until_ms", "attestation_json"),
            desc="get_remote_attestation",
            allow_none=True,
        )

        now = int(self._clock.time_msec())
        if row and now < row["valid_until_ms"]:
            defer.returnValue(json.loads(row["attestation_json"]))

        defer.returnValue(None)
