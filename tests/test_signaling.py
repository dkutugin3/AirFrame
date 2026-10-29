import asyncio
import time
import unittest

from aiohttp import WSMsgType
from aiohttp.test_utils import AioHTTPTestCase
from airframe.server import Room, create_app, validate_signal, video_settings


class SettingsTests(unittest.TestCase):
    def test_profiles(self):
        self.assertEqual(video_settings({}), {"fps": 120, "bitrate": 80, "maxSize": 2560})
        self.assertEqual(video_settings({"fps":60,"bitrate":24,"maxSize":2560})["fps"],60)
        self.assertEqual(video_settings({"fps":120,"bitrate":80})["fps"],120)
        for value in ({"fps":90},{"fps":True},{"bitrate":100},{"maxSize":999},[],None):
            with self.assertRaises(ValueError):video_settings(value)

    def test_volume_commands(self):
        self.assertTrue(validate_signal("receiver",{"type":"volume","value":0}))
        self.assertTrue(validate_signal("receiver",{"type":"volume","value":100}))
        for bad in ({"type":"volume","value":101},{"type":"volume","value":-1},
                    {"type":"volume","value":"50"},{"type":"volume"}):
            self.assertFalse(validate_signal("receiver",bad),bad)
        self.assertFalse(validate_signal("sender",{"type":"volume","value":50}))

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

    async def test_offer_answer_ice_and_reconnect(self):
        receiver,auth=await self.connect('receiver')
        self.assertEqual(auth['type'],'authenticated')
        sender,auth=await self.connect('sender')
        resume=auth['resumeToken']
        self.assertEqual((await receiver.receive_json(timeout=2))['type'],'peer-ready')
        self.assertEqual((await sender.receive_json(timeout=2))['settings']['fps'],120)
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
