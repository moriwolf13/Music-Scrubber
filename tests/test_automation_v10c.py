"""Regression coverage for v10c canonical album tags and safe working-copy writes."""
from contextlib import redirect_stdout
from io import StringIO, BytesIO
from pathlib import Path
from unittest.mock import patch
import hashlib, shutil
from PIL import Image
from mutagen import File
from mutagen.id3 import APIC
import mixie, tag, mixie_review_ui
from support import WorkspaceCase, make_audio, make_cover

class StabilizationV10CTests(WorkspaceCase):
    def rows(self,n=4):
        rows=[]
        for i in range(1,n+1):
            path=make_audio(self.stage/'Delivery'/f'{i:02d}.flac',album='Use Me  (Deluxe)',artist='PVRIS',
                title=f'Song {i}',tracknumber=i,discnumber=1,date='2020',genre='Pop')
            rows.append(tag.read_track_metadata(path))
        return {'album':'Use Me  (Deluxe)','tracks':rows}
    def test_deluxe_title_and_absent_album_artist(self):
        album=self.rows()
        fields=mixie._auto_album_metadata(album,{'album':'Use Me','album_artist':'PVRIS'},
                 {'tracklist':[{}]*len(album['tracks'])})
        self.assertEqual('Use Me (Deluxe)',fields['album'])
        self.assertEqual('PVRIS',fields['album_artist'])
    def test_single_different_album_label_follows_identified_album(self):
        album=self.rows();album['tracks'][0]['album']=('Use Me',)
        fields=mixie._auto_album_metadata(album,{'album':'Use Me','album_artist':'PVRIS'},
                  {'tracklist':[{}]*4})
        self.assertEqual('Use Me (Deluxe)',fields['album'])
    def test_original_date_prior_to_reissue_is_preserved(self):
        album=self.rows()
        for row in album['tracks']:row['date']=('2011-10-03',)
        # The original date must be independently supplied by release-group
        # evidence, not preferred merely because a download claims an older year.
        fields=mixie._auto_album_metadata(album,{'album':'Clash','date':'2011-10-03'},
                        {'tracklist':[{}]*4})
        self.assertEqual('2011-10-03',fields['date'])
    def test_one_wrong_genre_does_not_veto_album_consensus(self):
        album=self.rows();album['tracks'][-1]['genre']=('Jazz',)
        fields=mixie._auto_album_metadata(album,{'album':'Use Me'}, {'tracklist':[{}]*4})
        self.assertEqual('Pop',fields['genre'])
    def test_artist_genre_votes_count_albums_not_tracks(self):
        current={'tracks':[{'path':self.stage/'Delivery'/'current.flac'}]}
        other={}
        for i,genre in enumerate(('Electro Swing','Electro Swing','Electro Swing','Jazz'),1):
            other[str(i)]={'tracks':[{'path':self.library/f'{i}.flac','album':(f'Album {i}',),
                                   'artist':('Caravan Palace',),'genre':(genre,)}]}
        with patch.object(tag,'group_library_albums',return_value=other),patch.object(tag,'group_staged_albums',return_value={}):
            self.assertEqual('Electro Swing',mixie._catalog_genre_hint(current,'Caravan Palace'))
    def test_artist_genre_tie_does_not_guess(self):
        current={'tracks':[{'path':self.stage/'Delivery'/'current.flac'}]}
        other={}
        for i,genre in enumerate(('Jazz','Jazz','Rock','Rock'),1):
            other[str(i)]={'tracks':[{'path':self.library/f'{i}.flac','album':(f'Album {i}',),
                                   'artist':('Caravan Palace',),'genre':(genre,)}]}
        with patch.object(tag,'group_library_albums',return_value=other),patch.object(tag,'group_staged_albums',return_value={}):
            self.assertIsNone(mixie._catalog_genre_hint(current,'Caravan Palace'))
    def test_joint_artist_separator_is_specific(self):
        self.assertTrue(tag.joint_credit_equivalent('3OH!3_100 gecs','3OH!3 and 100 gecs'))
        self.assertFalse(tag.joint_credit_equivalent('3OH!3_100 gecs','3OH!3 and Ke$ha'))
    def test_replace_mp3_front_preserves_other_images_and_original(self):
        original=make_audio(self.stage/'Delivery'/'song.mp3',album='White Women',artist='Chromeo',
                            title='Song',tracknumber=1,discnumber=1,date='2014',genre='Funk')
        wrong=make_cover();a=File(original)
        a.tags.add(APIC(encoding=3,mime=wrong['mime'],type=3,desc='old',data=wrong['data']))
        a.tags.add(APIC(encoding=3,mime=wrong['mime'],type=4,desc='back',data=wrong['data']))
        a.save();before=hashlib.sha256(original.read_bytes()).hexdigest()
        work=self.root/'.mixie-work-test';work.mkdir();copy=work/'song.mp3';shutil.copy2(original,copy)
        buf=BytesIO();Image.new('RGB',(64,64),'navy').save(buf,format='PNG')
        good={'mime':'image/png','data':buf.getvalue()}
        tag.replace_reviewed_front_art_on_copy(copy,work,good)
        images=File(copy).tags.getall('APIC')
        self.assertEqual(1,len([p for p in images if p.type==3]))
        self.assertEqual(1,len([p for p in images if p.type==4]))
        self.assertEqual(before,hashlib.sha256(original.read_bytes()).hexdigest())
    def test_review_list_disambiguates_two_clash_imports(self):
        issues=[{'album':'Clash','import_name':'Source-A'}, {'album':'Clash','import_name':'Source-B'}]
        with patch('builtins.input',return_value='0'),redirect_stdout(StringIO()) as output:
            mixie_review_ui._list(mixie,issues,False)
        self.assertIn('source: Source-A',output.getvalue())
    def test_result_separates_verified_working_copy_and_archived(self):
        with redirect_stdout(StringIO()) as output:mixie._ingest_report(verified_library=3)
        self.assertIn('Success: 0',output.getvalue())
        self.assertIn('archive pending): 3',output.getvalue())
