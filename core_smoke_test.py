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
