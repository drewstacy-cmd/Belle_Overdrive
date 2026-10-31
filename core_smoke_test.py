import sys, types, importlib.util, tempfile, os, json
# Qt is not installed in this build environment. Stub only the GUI symbols so the
# non-GUI production subsystems can be imported and tested.
class Dummy:
    def __init__(self,*a,**k): pass
    def __getattr__(self,n): return lambda *a,**k: None
class Signal:
    def __init__(self,*a): pass
    def connect(self,*a,**k): pass
    def emit(self,*a,**k): pass
qtwidgets=types.ModuleType('PyQt5.QtWidgets')
for n in ['QApplication','QMainWindow','QWidget','QVBoxLayout','QHBoxLayout','QListWidget','QListWidgetItem','QLabel','QPushButton','QSlider','QComboBox','QSpinBox','QDoubleSpinBox','QSplitter','QFileDialog','QProgressBar','QMessageBox','QGroupBox','QFormLayout','QCheckBox']:
    setattr(qtwidgets,n,type(n,(Dummy,),{}))
qtcore=types.ModuleType('PyQt5.QtCore')
qtcore.Qt=type('Qt',(),{})
qtcore.QTimer=type('QTimer',(Dummy,),{})
qtcore.QThread=type('QThread',(Dummy,),{})
qtcore.pyqtSignal=lambda *a: Signal()
qtgui=types.ModuleType('PyQt5.QtGui')
qtgui.QImage=type('QImage',(Dummy,),{})
qtgui.QPixmap=type('QPixmap',(Dummy,),{})
pyqt=types.ModuleType('PyQt5')
pyqt.QtWidgets=qtwidgets; pyqt.QtCore=qtcore; pyqt.QtGui=qtgui
sys.modules.update({'PyQt5':pyqt,'PyQt5.QtWidgets':qtwidgets,'PyQt5.QtCore':qtcore,'PyQt5.QtGui':qtgui})
_assets_tmp=tempfile.mkdtemp(prefix='belle_assets_test_'); os.environ['BELLE_ASSET_ROOT']=_assets_tmp; os.environ['BELLE_SETTINGS']=os.path.join(_assets_tmp,'settings.json')
source_path=os.path.join(os.path.dirname(__file__),'Belle_Overdrive_Director.py')
spec=importlib.util.spec_from_file_location('belle',source_path)
b=importlib.util.module_from_spec(spec); sys.modules['belle']=b; spec.loader.exec_module(b)
# Parser and data validation
beats=b.BelleScriptChoreographer.load_screenplay('/nonexistent.pdf')
assert beats and len(beats)>5, len(beats)
errors,warnings=b.validate_project(beats)
assert not errors, errors[:5]
# Serialization round-trip including legacy format compatibility
payload=b.serialize_project(beats)
assert payload['format_version']==2
roundtrip=b.deserialize_project(payload)
assert len(roundtrip)==len(beats)
legacy=dict(payload); legacy['format_version']=1
assert len(b.deserialize_project(legacy))==len(beats)
# Frame interpolation and opacity
track=beats[0].tracks[0]
k=b.CinematicCompositor._interp(track.keyframes,0.5)
assert 0<=k.alpha<=1 and k.scale>0
# Deterministic audio and distinct cache signatures
with tempfile.TemporaryDirectory() as d:
    engine=b.ProceduralAudioEngine(d)
    one=engine.generate_sfx('PLASMA_BLAST',0.3)
    two=engine.generate_sfx('PLASMA_BLAST',0.3)
    assert one==two and os.path.exists(one)
    import wave
    with wave.open(one,'rb') as wf:
        assert wf.getframerate()==44100 and wf.getnframes()>0
# Render a frame and validate output dimensions
art=b.AnimeCelArtEngine(); comp=b.CinematicCompositor(art)
frame=comp.render_frame(beats[0],0.5)
assert frame.size==(1920,1080),frame.size
print('PASS: parser',len(beats),'beats; project v2 + v1 roundtrip; validation; interpolation; deterministic WAV; 1080p render')
# Portable sprite asset persistence
class FakeSelf: pass
with tempfile.TemporaryDirectory() as d:
    fake=FakeSelf(); fake.art=b.AnimeCelArtEngine()
    fake.art.custom_sprites['CORA']=__import__('PIL').Image.new('RGBA',(32,48),(20,30,40,255))
    paths=b._persist_sprite_assets(fake,os.path.join(d,'project.belle'))
    assert 'CORA' in paths and os.path.exists(os.path.join(d,paths['CORA']))
    payload=b.serialize_project(beats); payload['assets']={'sprites':paths}
    b._restore_sprite_assets(fake,os.path.join(d,'project.belle'),payload)
    assert fake.art.custom_sprites['CORA'].size==(32,48)
print('PASS: sprite assets persist with project-relative paths')

# ---- Integrations layer -------------------------------------------------------------
import belle_integrations as bi, zipfile, io, shutil, subprocess, copy
from PIL import Image as _Im
assert b.INTEGRATIONS is not None, 'integrations failed to install'
ctx=b.INTEGRATIONS
# Universal import: Krita/OpenRaster composites
def _png_bytes(size,color):
    buf=io.BytesIO(); _Im.new('RGBA',size,color).save(buf,'PNG'); return buf.getvalue()
with tempfile.TemporaryDirectory() as d:
    for ext in ('kra','ora'):
        p=os.path.join(d,'art.'+ext)
        with zipfile.ZipFile(p,'w') as z: z.writestr('mergedimage.png',_png_bytes((100,200),(9,9,9,255)))
        assert bi.load_any_image(p).size==(100,200)
assert bi.fit_sprite(_Im.new('RGBA',(2000,2000),(5,5,5,255))).height<=720
# Sprite clips: pose selection, talking variants, looping
root=ctx.root
for pose in ('IDLE','IDLE_TALK'):
    for i,color in ((1,(255,0,0,255)),(2,(0,0,255,255))):
        f=root/'sprites'/'CORA'/pose; f.mkdir(parents=True,exist_ok=True)
        _Im.new('RGBA',(40,60),color).save(f/f'frame_{i:04d}.png')
ctx.sprites.scan()
assert ctx.sprites.resolve('CORA','AIM',False)[0].parent.name=='IDLE'
assert ctx.sprites.resolve('CORA','IDLE',True)[0].parent.name=='IDLE_TALK'
fa=ctx.sprites.frame('CORA','IDLE',False,0.0); fb=ctx.sprites.frame('CORA','IDLE',False,1/12+0.001)
assert fa.getpixel((5,5))!=fb.getpixel((5,5))
art2=b.AnimeCelArtEngine(); comp2=b.CinematicCompositor(art2)
assert art2.render_character_rig('CORA','IDLE','DEFAULT',False,0.0).size==(40,60)
assert art2.render_character_rig('JULES','IDLE','DEFAULT',False,0.0).size==(480,720)  # procedural fallback intact
# Background plate override with push-in
_Im.new('RGB',(2400,1200),(30,60,90)).save(root/'backgrounds'/'DESERT.png')
ctx.backgrounds.scan()
bg=art2.render_background('DESERT','LONELY_EXT',0.5)
assert bg.size==(1920,1080) and bg.getpixel((10,10))[:3]==(30,60,90)
# Audio: deterministic mixdown; frame-snapped timeline
short=copy.deepcopy(beats[:2])
for bt in short: bt.duration=0.5
with tempfile.TemporaryDirectory() as d:
    eng=b.ProceduralAudioEngine(d)
    m1=bi.mixdown(short,ctx.audio,eng,24,os.path.join(d,'a.wav')); m2=bi.mixdown(short,ctx.audio,eng,24,os.path.join(d,'b.wav'))
    assert open(m1,'rb').read()==open(m2,'rb').read()
    assert [x[1:] for x in bi.beat_timeline(short,24)]==[(0,12),(12,24)]
    pk=__import__('pathlib').Path(d)/'pkg'; pk.mkdir()
    bi.write_srt(beats[:4],24,pk/'c.srt'); bi.write_markers_csv(short,24,pk/'m.csv'); bi.write_ae_jsx(short,24,pk)
    bi.write_premiere_xml(short,24,pk/'movie.mp4',pk/'s.xml')
    __import__('xml.dom.minidom',fromlist=['x']).parse(str(pk/'s.xml'))
    assert 'importFile' in (pk/'belle_import.jsx').read_text()
    compile(bi.BLENDER_SCRIPT,'blender_script','exec')
    if ctx.tools.executable('ffmpeg'):
        out=os.path.join(d,'out.mp4')
        res=b.StudioExporter.export_mp4(short,comp,eng,out,fps=24)
        probe=subprocess.run([ctx.tools.executable('ffmpeg'),'-hide_banner','-i',res],capture_output=True,text=True).stderr
        assert 'h264' in probe and 'aac' in probe and '1920x1080' in probe, probe[-400:]
        clip_src=os.path.join(d,'clip.mp4')
        subprocess.run([ctx.tools.executable('ffmpeg'),'-y','-loglevel','error','-f','lavfi','-i','testsrc=size=160x240:rate=12','-t','1',clip_src],check=True)
        msg=bi.render_clip(ctx,clip_src,'LEO','ANY',fps=12)
        ctx.sprites.scan(); assert ctx.sprites.resolve('LEO','IDLE',False) and '12 frames' in msg, msg
print('PASS: integrations (import formats, sprite clips, backgrounds, mixdown, AE/Premiere packages, FFmpeg export, clip render)')
