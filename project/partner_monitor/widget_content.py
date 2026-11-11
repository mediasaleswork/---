"""Validate the message body in a Telegram embed against the authorized original."""
import re
import unicodedata

from PIL import Image, ImageStat

from .message_content import message_content, visual_sources


def expected_content(message):
    visuals = list(visual_sources(message))
    document = getattr(message, 'document', None)
    video = bool(document and (getattr(document, 'mime_type', '') or '').startswith('video/'))
    return {'text': message_content(message).text, 'visuals': max(len(visuals), int(video))}


def words(text):
    return ''.join(re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold()))


async def read_body(post):
    return await post.evaluate(r"""async root => {
      const shown = el => {
        const b=el.getBoundingClientRect(), s=getComputedStyle(el);
        return b.width>0 && b.height>0 && s.visibility!=='hidden' && s.display!=='none' && Number(s.opacity)>0;
      };
      const nodes = selector => [...root.querySelectorAll(selector)].filter(shown);
      const textNodes = nodes('.tgme_widget_message_text');
      const panels = nodes('.tgme_widget_message_photo_wrap,.tgme_widget_message_video_player,.tgme_widget_message_sticker');
      const extras = nodes('.tgme_widget_message_document,.tgme_widget_message_poll,.tgme_widget_message_voice_player,.tgme_widget_message_link_preview');
      const blocked = nodes('.message_media_not_supported_label,.tgme_widget_message_error')
          .some(el=>el.innerText.trim().length>0);
      const box=root.getBoundingClientRect();
      const geometry=el=>{const b=el.getBoundingClientRect();return [b.x-box.x,b.y-box.y,b.width,b.height];};
      async function decoded(panel) {
        const urls=new Set();
        for(const el of [panel,...panel.querySelectorAll('*')].filter(shown)){
          if (el instanceof HTMLImageElement && el.currentSrc) urls.add(el.currentSrc);
          if (el instanceof HTMLVideoElement && el.poster) urls.add(el.poster);
          for (const value of getComputedStyle(el).backgroundImage.matchAll(/url\(["']?(.*?)["']?\)/g)) urls.add(value[1]);
        }
        if(!urls.size)return [...panel.querySelectorAll('video')].some(v=>v.readyState>=2 && v.videoWidth>12);
        const loaded=await Promise.race([
          Promise.all([...urls].map(async url=>{const im=new Image();im.src=url;
             try{await im.decode();return im.naturalWidth>=12 && im.naturalHeight>=12;}catch{return false;}})),
          new Promise(resolve=>setTimeout(()=>resolve(null),6000))
        ]);
        return !!loaded && loaded.every(Boolean);
      }
      const decodedMedia=await Promise.all(panels.map(decoded));
      return {text:textNodes.map(n=>n.innerText).join(' '), blocked,
        visuals:panels.length, broken:decodedMedia.some(ok=>!ok), extras:extras.length,
        regions:[...textNodes,...panels,...extras].map(geometry), mediaRegions:panels.map(geometry)};
    }""")


def acceptable(body, expected=None):
    text = words(body['text'])
    if body['blocked'] or body['broken'] or not (body['text'].strip() or body['visuals'] or body['extras']):
        return False
    if expected:
        required = words(expected['text'])
        if required and required not in text:
            return False
        if body['visuals'] < expected['visuals']:
            return False
    return True


def visible_pixels(path, body):
    with Image.open(path) as bitmap:
        def has_detail(rect):
            x, y, w, h = rect
            area = bitmap.crop((max(0, round(x)), max(0, round(y)),
                                min(bitmap.width, round(x+w)), min(bitmap.height, round(y+h))))
            if min(area.size) < 2:
                return False
            low, high = ImageStat.Stat(area.convert('L')).extrema[0]
            return high-low >= 12
        return (any(has_detail(r) for r in body['regions'])
                and all(has_detail(r) for r in body['mediaRegions']))
