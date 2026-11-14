import json
import unittest
from pathlib import Path
from report import validate,three_stars,render_card
ROOT=Path(__file__).resolve().parent.parent
class Tests(unittest.TestCase):
    def setUp(self):self.game=json.loads((ROOT/'game.example.json').read_text())
    def test_validation(self):self.assertEqual(validate(self.game)['status'],'final/ot')
    def test_stars(self):
        stars=three_stars(self.game)
        self.assertEqual(len(stars),3)
        self.assertEqual(stars[0]['name'],'Example Forward A')
    def test_card(self):self.assertTrue(render_card(self.game).startswith(b'\x89PNG'))
    def test_no_stats(self):
        self.game['players']=[]
        self.assertEqual(three_stars(self.game),[])
    def test_no_unfinished(self):
        self.game['status']='scheduled'
        with self.assertRaises(ValueError):validate(self.game)
if __name__=='__main__':unittest.main()
