"""Extract approved logo pixels, then convert to Windows resource formats.
No generative drawing or replacement logo: source rectangles are recorded below.
"""
from pathlib import Path
import hashlib
import json
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / 'resources'

def generate():
    src = RES / 'reference' / 'identity-approved.png'
    identity = Image.open(src).convert('RGB')
    # The upper-left standalone NV mark, excluding separator and wordmark.
    crop = identity.crop((88, 5, 352, 302))
    rgb = np.asarray(crop).astype(float)
    # Recover antialiased coverage against the near-white paper background.
    alpha = np.clip((245 - rgb[:, :, 0]) / (245 - 5), 0, 1)
    alpha[rgb[:, :, 0] > 190] = 0
    navy = Image.new('RGBA', crop.size, '#082f4c')
    navy.putalpha(Image.fromarray((alpha * 255).astype('uint8')))
    navy.save(RES / 'logo-navy.png')
    white = Image.new('RGBA', crop.size, 'white')
    white.putalpha(navy.getchannel('A'))
    white.save(RES / 'logo-white.png')
    tile = Image.new('RGBA', (512, 512))
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle((5, 5, 507, 507), radius=95, fill='#0b3a66')
    mark = white.resize((342, 385), Image.Resampling.LANCZOS)
    tile.alpha_composite(mark, (85, 63))
    tile.save(RES / 'navmodel.png')
    tile.save(RES / 'navmodel.ico', sizes=[(s,s) for s in (16,24,32,48,64,128,256)])
    # Retain only the industrial wireframe from the approved splash, no UI pixels.
    source = Image.open(RES / 'reference' / 'splash-approved.png').convert('RGB')
    art = source.crop((630, 140, 1480, 735))
    a = np.asarray(art).astype(float)
    strength = np.clip((a[:, :, 2] - 75) / 115, 0, 0.42)
    strength *= np.minimum(np.arange(art.width)[None, :] / 100, 1)
    wire = Image.new('RGBA', art.size, '#a5c9e9')
    wire.putalpha(Image.fromarray((strength * 255).astype('uint8')))
    wire.save(RES / 'splash-art.png')
    wizard = Image.new('RGB', (164, 314), '#0b3a66')
    icon = tile.resize((112,112), Image.Resampling.LANCZOS)
    wizard.paste(icon, (26,52), icon)
    draw = ImageDraw.Draw(wizard)
    font = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', 18)
    draw.text((28,190), 'NavModel', fill='white', font=font)
    draw.text((28,218), 'MANAGER', fill='white', font=font)
    wizard.save(RES / 'installer_wizard.bmp')
    small = Image.new('RGB', (55,55), 'white')
    icon = tile.resize((55,55), Image.Resampling.LANCZOS)
    small.paste(icon, (0,0), icon)
    small.save(RES / 'installer_banner.bmp')
    (RES / 'asset-provenance.json').write_text(json.dumps({
        'logo_source': src.name, 'logo_crop': [88,5,352,302],
        'logo_source_sha256': hashlib.sha256(src.read_bytes()).hexdigest(),
        'wireframe_source': 'splash-approved.png', 'wireframe_crop': [630,140,1480,735],
        'method': 'Extract approved raster silhouette; remove paper background; resize. No redraw.'
    }, indent=2), encoding='utf-8')

if __name__ == '__main__':
    generate()
