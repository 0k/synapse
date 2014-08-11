# -*- coding: utf-8 -*-
# twisted imports
from twisted.internet import defer

# trial imports
from twisted.trial import unittest

from synapse.api.constants import Membership

import time

class RestTestCase(unittest.TestCase):
    """Contains extra helper functions to quickly and clearly perform a given
    REST action, which isn't the focus of the test.

    This subclass assumes there are mock_server and auth_user_id attributes.
    """

    def __init__(self, *args, **kwargs):
        super(RestTestCase, self).__init__(*args, **kwargs)
        self.mock_server = None
        self.auth_user_id = None

    def mock_get_user_by_token(self, token=None):
        return self.auth_user_id

    @defer.inlineCallbacks
    def create_room_as(self, room_id, room_creator, is_public=True, tok=None):
        temp_id = self.auth_user_id
        self.auth_user_id = room_creator
        path = "/rooms/%s" % room_id
        content = "{}"
        if not is_public:
            content = '{"visibility":"private"}'
        if tok:
            path = path + "?access_token=%s" % tok
        (code, response) = yield self.mock_server.trigger("PUT", path, content)
        self.assertEquals(200, code, msg=str(response))
        self.auth_user_id = temp_id

    @defer.inlineCallbacks
    def invite(self, room=None, src=None, targ=None, expect_code=200, tok=None):
        yield self.change_membership(room=room, src=src, targ=targ, tok=tok,
                                     membership=Membership.INVITE,
                                     expect_code=expect_code)

    @defer.inlineCallbacks
    def join(self, room=None, user=None, expect_code=200, tok=None):
        yield self.change_membership(room=room, src=user, targ=user, tok=tok,
                                     membership=Membership.JOIN,
                                     expect_code=expect_code)

    @defer.inlineCallbacks
    def leave(self, room=None, user=None, expect_code=200, tok=None):
        yield self.change_membership(room=room, src=user, targ=user, tok=tok,
                                     membership=Membership.LEAVE,
                                     expect_code=expect_code)

    @defer.inlineCallbacks
    def change_membership(self, room=None, src=None, targ=None,
                          membership=None, expect_code=200, tok=None):
        temp_id = self.auth_user_id
        self.auth_user_id = src

        path = "/rooms/%s/members/%s/state" % (room, targ)
        if tok:
            path = path + "?access_token=%s" % tok

        if membership == Membership.LEAVE:
            (code, response) = yield self.mock_server.trigger("DELETE", path,
                                    None)
            self.assertEquals(expect_code, code, msg=str(response))
        else:
            (code, response) = yield self.mock_server.trigger("PUT", path,
                                    '{"membership":"%s"}' % membership)
            self.assertEquals(expect_code, code, msg=str(response))

        self.auth_user_id = temp_id

    @defer.inlineCallbacks
    def register(self, user_id):
        (code, response) = yield self.mock_server.trigger("POST", "/register",
                                '{"user_id":"%s"}' % user_id)
        self.assertEquals(200, code)
        defer.returnValue(response)

    @defer.inlineCallbacks
    def send(self, room_id, sender_id, body=None, msg_id=None, tok=None,
             expect_code=200):
        if msg_id is None:
            msg_id = "m%s" % (str(time.time()))
        if body is None:
            body = "body_text_here"

        path = "/rooms/%s/messages/%s/%s" % (room_id, sender_id, msg_id)
        content = '{"msgtype":"m.text","body":"%s"}' % body
        if tok:
            path = path + "?access_token=%s" % tok

        (code, response) = yield self.mock_server.trigger("PUT", path, content)
        self.assertEquals(expect_code, code, msg=str(response))

    def assert_dict(self, required, actual):
        """Does a partial assert of a dict.

        Args:
            required (dict): The keys and value which MUST be in 'actual'.
            actual (dict): The test result. Extra keys will not be checked.
        """
        for key in required:
            self.assertEquals(required[key], actual[key],
                              msg="%s mismatch. %s" % (key, actual))
