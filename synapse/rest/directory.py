# -*- coding: utf-8 -*

from twisted.internet import defer

from synapse.types import RoomAlias, RoomID
from base import RestServlet, InvalidHttpRequestError, client_path_pattern

import json
import logging
import urllib


logger = logging.getLogger(__name__)


def register_servlets(hs, http_server):
    ClientDirectoryServer(hs).register(http_server)


class ClientDirectoryServer(RestServlet):
    PATTERN = client_path_pattern("^/ds/room/(?P<room_alias>[^/]*)$")

    @defer.inlineCallbacks
    def on_GET(self, request, room_alias):
        # TODO(erikj): Handle request
        local_only = "local_only" in request.args

        room_alias = urllib.unquote(room_alias)
        room_alias_obj = RoomAlias.from_string(room_alias, self.hs)

        dir_handler = self.handlers.directory_handler
        res = yield dir_handler.get_association(
            room_alias_obj,
            local_only=local_only
        )

        defer.returnValue((200, res))

    @defer.inlineCallbacks
    def on_PUT(self, request, room_alias):
        # TODO(erikj): Exceptions
        content = json.loads(request.content.read())

        logger.debug("Got content: %s", content)

        room_alias = urllib.unquote(room_alias)
        room_alias_obj = RoomAlias.from_string(room_alias, self.hs)

        logger.debug("Got room name: %s", room_alias_obj.to_string())

        room_id = content["room_id"]
        servers = content["servers"]

        logger.debug("Got room_id: %s", room_id)
        logger.debug("Got servers: %s", servers)

        # TODO(erikj): Check types.
        # TODO(erikj): Check that room exists

        dir_handler = self.handlers.directory_handler

        try:
            yield dir_handler.create_association(
                room_alias_obj, room_id, servers
            )
        except:
            logger.exception("Failed to create association")

        defer.returnValue((200, {}))
