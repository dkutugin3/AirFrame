import asyncio
import time
import unittest

from aiohttp import WSMsgType
from aiohttp.test_utils import AioHTTPTestCase
from airframe.server import Room, create_app, validate_signal, video_settings


class SettingsTests(unittest.TestCase):
    def test_profiles(self):
        # Stage 1 behaviour change: the base profile of a new session is
        # 60 fps / 24 Mbit/s / 2560 px. The previous expectation (120/80/2560)
        # encoded the audit defect, not desired behaviour. The experimental
        # 120 fps preset stays a valid explicit choice; it is just not a default.
        self.assertEqual(video_settings({}), {"fps": 60, "bitrate": 24, "maxSize": 2560})
        self.assertEqual(video_settings({"fps":60,"bitrate":24,"maxSize":2560})["fps"],60)
        self.assertEqual(video_settings({"fps":120,"bitrate":80})["fps"],120)
        # The lighter 60/16/1920 profile must remain available for weaker machines.
        self.assertEqual(video_settings({"fps":60,"bitrate":16,"maxSize":1920}),
                         {"fps":60,"bitrate":16,"maxSize":1920})
        for value in ({"fps":90},{"fps":True},{"bitrate":100},{"maxSize":999},[],None):
            with self.assertRaises(ValueError):video_settings(value)

    def test_volume_commands(self):
        self.assertTrue(validate_signal("receiver",{"type":"volume","value":0}))
        self.assertTrue(validate_signal("receiver",{"type":"volume","value":100}))
        for bad in ({"type":"volume","value":101},{"type":"volume","value":-1},
                    {"type":"volume","value":"50"},{"type":"volume"}):
            self.assertFalse(validate_signal("receiver",bad),bad)
        self.assertFalse(validate_signal("sender",{"type":"volume","value":50}))

    def test_audio_commands(self):
        for enabled in (True,False):
            self.assertTrue(validate_signal("receiver",{"type":"audio","enabled":enabled}))
        for bad in ({"type":"audio","enabled":"yes"},{"type":"audio"},{"type":"audio","enabled":1}):
            self.assertFalse(validate_signal("receiver",bad),bad)
        self.assertFalse(validate_signal("sender",{"type":"audio","enabled":True}))

    def test_code_expiry_and_strong_reconnect_token(self):
        room=Room(); code=room.code
        self.assertTrue(room.authenticate("sender",code,"phone"))
        self.assertNotEqual(room.code,code)
        self.assertTrue(room.authenticate("sender",room.resume_token,"phone"))
        room.code_until=time.monotonic()-1
        self.assertFalse(room.authenticate("sender",room.code,"other"))

    def test_bad_pairing_attempts_are_rate_limited(self):
        room=Room()
        wrong="bad"
        for _ in range(5):self.assertFalse(room.authenticate("sender",wrong,"phone"))
        self.assertFalse(room.authenticate("sender",room.code,"phone"))


class SignalingTests(AioHTTPTestCase):
    async def get_application(self):
        self.room=Room()
        return create_app(self.room)

    async def connect(self,role,credential=None):
        ws=await self.client.ws_connect('/signal')
        await ws.send_json({"role":role,"credential":credential or (self.room.receiver_token if role=="receiver" else self.room.code)})
        response=await ws.receive_json(timeout=2)
        return ws,response

    async def test_secret_api_requires_authorization(self):
        response=await self.client.get('/api/session')
        self.assertEqual(response.status,401)
        response=await self.client.get('/api/session',headers={"Authorization":f"Bearer {self.room.receiver_token}"})
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())["code"],self.room.code)

    async def test_cross_origin_and_unknown_assets_rejected(self):
        response=await self.client.get('/api/session',headers={"Origin":"https://attacker.example"})
        self.assertEqual(response.status,403)
        self.assertEqual((await self.client.get('/assets/secrets.txt')).status,404)

    async def test_new_session_requests_quality_60_profile(self):
        # RED (stage 1): a real connection must ask the phone for the base
        # profile of a new session: 60 fps, at most 24 Mbit/s, 2560 px long side.
        # 24 Mbit/s is an upper bound, not a promised actual bitrate.
        base={"fps":60,"bitrate":24,"maxSize":2560}
        receiver,auth=await self.connect('receiver')
        self.assertEqual(auth['type'],'authenticated')
        sender,auth=await self.connect('sender')
        self.assertEqual(auth['type'],'authenticated')
        self.assertEqual((await receiver.receive_json(timeout=2))['type'],'peer-ready')
        ready=await sender.receive_json(timeout=2)
        self.assertEqual(ready['type'],'peer-ready')
        self.assertEqual(ready['settings'],base)
        # The pairing API advertises exactly the profile the phone is asked to open with.
        session=await (await self.client.get(
            '/api/session',headers={"Authorization":f"Bearer {self.room.receiver_token}"})).json()
        self.assertEqual(session['settings'],ready['settings'])
        await sender.close();await receiver.close()

    async def test_offer_answer_ice_and_reconnect(self):
        receiver,auth=await self.connect('receiver')
        self.assertEqual(auth['type'],'authenticated')
        sender,auth=await self.connect('sender')
        resume=auth['resumeToken']
        self.assertEqual((await receiver.receive_json(timeout=2))['type'],'peer-ready')
        # Stage 1: a new session opens with the base profile (60 fps), not the
        # old 120 fps audit defect. The old expectation here encoded the bug.
        self.assertEqual((await sender.receive_json(timeout=2))['settings'],
                         {"fps":60,"bitrate":24,"maxSize":2560})
        await sender.send_json({'type':'offer','session':'1','sdp':'offer-sdp'})
        self.assertEqual((await receiver.receive_json(timeout=2))['sdp'],'offer-sdp')
        await receiver.send_json({'type':'answer','session':'1','sdp':'answer-sdp'})
        self.assertEqual((await sender.receive_json(timeout=2))['sdp'],'answer-sdp')
        ice={'type':'ice','session':'1','candidate':'candidate:1 1 udp 1 192.168.1.2 1234 typ host','sdpMid':'0','sdpMLineIndex':0}
        await sender.send_json(ice)
        self.assertEqual(await receiver.receive_json(timeout=2),ice)
        await sender.close()
        self.assertEqual((await receiver.receive_json(timeout=2))['type'],'peer-left')
        sender,auth=await self.connect('sender',resume)
        self.assertEqual(auth['type'],'authenticated')
        await sender.close();await receiver.close()

    async def test_invalid_code_and_duplicate_receiver(self):
        sender,auth=await self.connect('sender','incorrect')
        self.assertEqual(auth['type'],'error');await sender.close()
        receiver,_=await self.connect('receiver')
        duplicate,auth=await self.connect('receiver')
        self.assertEqual(auth['type'],'error')
        await duplicate.close();await receiver.close()

    async def test_configure_stop_and_role_restriction(self):
        receiver,_=await self.connect('receiver');sender,_=await self.connect('sender')
        await receiver.receive_json(timeout=2);await sender.receive_json(timeout=2)
        await receiver.send_json({'type':'configure','settings':{'fps':60,'bitrate':16,'maxSize':1920}})
        self.assertEqual((await sender.receive_json(timeout=2))['settings']['fps'],60)
        await receiver.send_json({'type':'stop'})
        self.assertEqual((await sender.receive_json(timeout=2))['type'],'stop')
        await sender.send_json({'type':'configure','settings':{}})
        self.assertEqual((await sender.receive(timeout=2)).type,WSMsgType.CLOSE)
        await sender.close();await receiver.close()

    async def test_invalid_json_does_not_leave_ghost_peer(self):
        sender,_=await self.connect('sender')
        await sender.send_str('{malformed')
        self.assertEqual((await sender.receive(timeout=2)).type,WSMsgType.CLOSE)
        await sender.close()
        await asyncio.sleep(0.01)
        self.assertNotIn('sender',self.room.peers)


if __name__=='__main__':unittest.main()

class QualityProtocolTests(unittest.TestCase):
    def test_qr_rotates_expires_and_resumes(self):
        room=Room();key=room.qr_token
        self.assertEqual(len(key),32)
        self.assertTrue(room.authenticate('sender',key,'qr-phone'))
        self.assertNotEqual(key,room.qr_token)
        self.assertFalse(room.authenticate('sender',key,'qr-phone'))
        self.assertTrue(room.authenticate('sender',room.resume_token,'qr-phone'))
        room.code_until=0
        self.assertFalse(room.authenticate('sender',room.qr_token,'other'))

    def test_telemetry_role_and_bounds(self):
        from airframe.server import validate_signal
        message={'type':'telemetry','session':'1','encodedFps':60,'width':1920,'height':880,'limitation':'none'}
        self.assertTrue(validate_signal('sender',message))
        self.assertFalse(validate_signal('receiver',message))
        self.assertFalse(validate_signal('sender',{**message,'encodedFps':float('nan')}))
        self.assertFalse(validate_signal('sender',{**message,'width':-1}))

class QualitySignalingTests(AioHTTPTestCase):
    get_application = SignalingTests.get_application
    connect = SignalingTests.connect
    async def test_apply_confirmation_roundtrip(self):
        receiver,_=await self.connect('receiver');sender,_=await self.connect('sender')
        await receiver.receive_json(timeout=2);await sender.receive_json(timeout=2)
        settings={'fps':60,'bitrate':30,'maxSize':1920}
        await receiver.send_json({'type':'configure','settings':settings,'requestId':'click-1'})
        configured=await sender.receive_json(timeout=2)
        self.assertEqual(configured['requestId'],'click-1')
        self.assertEqual(configured['settings'],settings)
        ack={'type':'configured','requestId':'click-1','applied':True,'settings':settings}
        await sender.send_json(ack)
        self.assertEqual(await receiver.receive_json(timeout=2),ack)
        await sender.close();await receiver.close()


class ProfileAckSignalingTests(AioHTTPTestCase):
    """Stage 1: the session profile must follow what the phone actually acknowledged.

    Requested != effective when the phone rejects: the session must keep the
    previously effective profile instead of advertising the wish as applied.
    """
    get_application = SignalingTests.get_application
    connect = SignalingTests.connect

    async def pair(self):
        receiver,_=await self.connect('receiver')
        sender,_=await self.connect('sender')
        await receiver.receive_json(timeout=2)  # peer-ready
        await sender.receive_json(timeout=2)    # peer-ready with settings
        return receiver,sender

    async def test_rejected_configure_keeps_previous_effective_profile(self):
        receiver,sender=await self.pair()
        try:
            previous=dict(self.room.settings)
            requested={'fps':30,'bitrate':10,'maxSize':1920}
            await receiver.send_json({'type':'configure','settings':requested,'requestId':'profile-1'})
            self.assertEqual((await sender.receive_json(timeout=2))['settings'],requested)
            # The saved profile may already hold the wish for the next session,
            # but a rejection must restore what is still effectively running.
            await sender.send_json({'type':'configured','requestId':'profile-1',
                                    'applied':False,'settings':requested})
            ack=await receiver.receive_json(timeout=2)
            self.assertEqual(ack['requestId'],'profile-1')
            self.assertFalse(ack['applied'])
            self.assertEqual(self.room.settings,previous)
        finally:
            await sender.close();await receiver.close()

    async def test_applied_configure_adopts_acked_settings(self):
        receiver,sender=await self.pair()
        try:
            requested={'fps':120,'bitrate':80,'maxSize':2560}
            await receiver.send_json({'type':'configure','settings':requested,'requestId':'profile-2'})
            await sender.receive_json(timeout=2)
            acked={'fps':60,'bitrate':16,'maxSize':1920}
            await sender.send_json({'type':'configured','requestId':'profile-2',
                                    'applied':True,'settings':acked})
            ack=await receiver.receive_json(timeout=2)
            self.assertTrue(ack['applied'])
            # The phone's acknowledged settings are the effective profile,
            # not the requested wish (the phone may clamp values).
            self.assertEqual(self.room.settings,acked)
        finally:
            await sender.close();await receiver.close()

    async def test_late_duplicate_ack_cannot_roll_back_profile(self):
        receiver,sender=await self.pair()
        try:
            requested={'fps':120,'bitrate':80,'maxSize':2560}
            acked={'fps':60,'bitrate':24,'maxSize':2560}
            await receiver.send_json({'type':'configure','settings':requested,'requestId':'profile-3'})
            await sender.receive_json(timeout=2)
            await sender.send_json({'type':'configured','requestId':'profile-3',
                                    'applied':True,'settings':acked})
            await receiver.receive_json(timeout=2)
            self.assertEqual(self.room.settings,acked)
            # A duplicate/late acknowledgement for the same requestId must not
            # overwrite the profile that was already accepted.
            await sender.send_json({'type':'configured','requestId':'profile-3',
                                    'applied':True,'settings':requested})
            await receiver.receive_json(timeout=2)
            self.assertEqual(self.room.settings,acked)
        finally:
            await sender.close();await receiver.close()

    async def test_ack_for_unknown_request_does_not_change_profile(self):
        receiver,sender=await self.pair()
        try:
            previous=dict(self.room.settings)
            sender_ack={'type':'configured','requestId':'never-requested',
                        'applied':True,'settings':{'fps':120,'bitrate':80,'maxSize':2560}}
            await sender.send_json(sender_ack)
            await receiver.receive_json(timeout=2)
            self.assertEqual(self.room.settings,previous)
        finally:
            await sender.close();await receiver.close()
