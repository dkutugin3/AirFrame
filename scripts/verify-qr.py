"""Decode native QR pixels in Docker without printing the pairing secret."""
from PIL import Image
import zxingcpp
from urllib.parse import urlsplit,parse_qs
from pathlib import Path
import sys
result=zxingcpp.read_barcode(Image.open(sys.argv[1]))
assert result is not None, 'Native QR image could not be decoded'
uri=urlsplit(result.text)
values=parse_qs(uri.query,strict_parsing=True)
assert uri.scheme=='airframe' and uri.netloc=='connect'
assert values['v']==['1'] and len(values['key'][0])==32
print('PASS: ZXing decodes native Qt QR pixels and recognizes the pairing payload')
