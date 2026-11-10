"""Classical image features and text identities, using generated local fixtures."""
import io
import random
import unittest

from PIL import Image,ImageDraw

from partner_monitor.campaigns import image_input
from partner_monitor.campaign_local import conflicting_numbers,equivalent_matches,text_copy
from partner_monitor.campaign_visual import matching_artwork


def artwork(seed=42):
    rng=random.Random(seed)
    picture=Image.new('RGB',(700,420),'white');draw=ImageDraw.Draw(picture)
    for i in range(100):
        x=rng.randrange(650);y=rng.randrange(380)
        draw.rectangle((x,y,x+rng.randrange(10,50),y+rng.randrange(10,40)),fill=tuple(rng.randrange(256) for _ in range(3)))
        draw.text((x,y),str(i),fill='black')
    return picture


def part(picture,quality=90):
    stream=io.BytesIO();picture.save(stream,format='JPEG',quality=quality)
    return image_input(stream.getvalue())


class VisualTests(unittest.TestCase):
    def test_resizing_and_jpeg_recompression_match(self):
        source=artwork()
        self.assertTrue(matching_artwork([part(source.resize((350,210)),45)],[part(source)]))

    def test_crop_and_watermark_match(self):
        source=artwork();crop=source.crop((45,25,660,400))
        ImageDraw.Draw(crop).rectangle((0,0,100,20),fill='white')
        self.assertTrue(matching_artwork([part(crop)],[part(source)]))

    def test_unrelated_artwork_and_corner_only_do_not_match(self):
        source=artwork()
        self.assertFalse(matching_artwork([part(artwork(999))],[part(source)]))
        self.assertFalse(matching_artwork([part(source.crop((0,0,140,90)))],[part(source)]))

    def test_video_alternatives_choose_one_but_two_different_videos_remain_ambiguous(self):
        refs=[{'id':1,'client_name':'BRB','ad_type':'рекламный материал','source_text':'https://youtu.be/AbCdEfGhI_1'},
              {'id':2,'client_name':'BRB','ad_type':'рекламный материал','source_text':'https://youtube.com/watch?v=AbCdEfGhI_1'}]
        self.assertEqual(set(equivalent_matches({1:'video',2:'video'},refs)),{1})
        refs[1]['source_text']='https://youtu.be/AbCdEfGhI_2'
        self.assertEqual(len(equivalent_matches({1:'video',2:'video'},refs)),2)

    def test_album_with_two_different_creatives_is_not_collapsed(self):
        refs=[{'id':1,'client_name':'BRB','source_text':'Первый материал'}, {'id':2,'client_name':'BRB','source_text':'Второй материал'}]
        self.assertEqual(len(equivalent_matches({1:'image',2:'image'},refs,{1:{0},2:{1}})),2)

    def test_actual_offer_numbers_conflict_but_tracking_parameters_do_not(self):
        ref={'ad_type':'рекламный материал','source_text':'Ставка 12 процентов. https://youtube.com/watch?v=abc&t=44s'}
        self.assertTrue(conflicting_numbers('Ставка 25 процентов',ref))
        self.assertFalse(conflicting_numbers('Ставка 12 процентов https://youtu.be/abc?t=120',ref))

    def test_uzbek_script_and_removed_url_are_formatting_changes(self):
        source='Бизнесни ривожлантириш банки Ўзбекистонда тадбиркорлик ва савдо сотиқ тарихига бағишланган қизиқарли ҳужжатли фильмни тақдим этибди. Буюк Ипак йўли давридаги савдо карвонлари. https://youtu.be/AbCdEfGhI_1?t=44'
        latin='Biznesni rivojlantirish banki Ozbekistonda tadbirkorlik va savdo sotiq tarixiga bagishlangan qiziqarli hujjatli filmni taqdim etibdi. Buyuk Ipak yoli davridagi savdo karvonlari.'
        self.assertTrue(text_copy(latin,{'ad_type':'рекламный материал','source_text':source}))
