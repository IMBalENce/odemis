# -*- mode: python -*-

import os

from PyInstaller.utils.hooks import collect_submodules


def get_lib_tiff():
    """ Help PyInstaller find all the lib-tiff files it needs """
    import os

    print("Looking for libtiff")
    import libtiff
    tiff_path = os.path.dirname(libtiff.__file__)

    if os.path.isfile(os.path.join(tiff_path, 'libtiff.dll')) and \
    		os.path.isfile(os.path.join(tiff_path, 'tiff.h')):
        return [
            ('libtiff.dll', os.path.join(tiff_path, 'libtiff.dll'), 'DATA'),
            ('tiff.h', os.path.join(tiff_path, 'tiff.h'), 'DATA')
        ]

    raise ImportError("Could not find Libtiff files!")


def get_cairo_dlls():
    """ Help PyInstaller find all the Cairo files it needs """
    import os

    dlls = [
        "freetype6.dll",
        "libcairo-2.dll",
        "libexpat-1.dll",
        "libfontconfig-1.dll",
        "libpng14-14.dll"
    ]

    dll_path = '.\\bin\\dll'

    if all(os.path.exists(os.path.join(dll_path, dll)) for dll in dlls):
        return [(dll, os.path.join(dll_path, dll), 'DATA') for dll in dlls]

    dll_path = os.environ['WINDIR'] + "\\SysWOW64"

    if all(os.path.exists(os.path.join(dll_path, dll)) for dll in dlls):
        return [(dll, os.path.join(dll_path, dll), 'DATA') for dll in dlls]

    dll_path = os.environ['WINDIR'] + "\\System32"

    if all(os.path.exists(os.path.join(dll_path, dll)) for dll in dlls):
        return [(dll, os.path.join(dll_path, dll), 'DATA') for dll in dlls]

    raise ImportError("Could not find Cairo files!")


def get_dataio_imports():
    import odemis.dataio

    imports = []
    for module in odemis.dataio._iomodules:
        imports.append("odemis.dataio.%s" % module)
    return imports


def get_wx_imports():
    # Special hooks-wx-lib.pubsub doesn't work properly if these modules are not 
    # collected explicitly
    return collect_submodules('pubsub.core.kwargs') + \
            collect_submodules('pubsub.core.arg1') + \
            ["pubsub.core.publisherbase", "pubsub.core.listenerbase" ]
    # Put it back, once wxpython 4 has an internal pubsub again
    #return collect_submodules('wx.lib.pubsub.core.kwargs') + \
    #	   collect_submodules('wx.lib.pubsub.core.arg1') + \
    #	   ["wx.lib.pubsub.core.publisherbase", "wx.lib.pubsub.core.listenerbase" ]

def get_libtiff_imports():
    # tiff_h_x_y_z needs administrator rights to be generated, so import it here
    import libtiff
    h_file = libtiff.libtiff_ctypes.tiff_h_name
    return ["libtiff.%s" % (h_file,)]

def get_version():
    """ Write the current version of Odemis to a txt file and tell PyInstaller where to find it """
    import odemis

    with open('dist/version.txt', 'w') as f:
        long_version = odemis.get_version_simplified()
        f.write(long_version + '\n')
    return [('version.txt', 'dist/version.txt', 'DATA')]


def get_gui_img():
    """ Create data for all images in odemis.gui.img """

    IMG_MATCH = ('.png', '.jpg', '.ico')

    def rec_glob(p, gfiles):
        import glob
        for d in glob.glob(p):
            if d.lower().endswith(IMG_MATCH):
                gfiles.append(d)
            rec_glob("%s/*" % d, gfiles)

    files = []
    rec_glob("../../src/odemis/gui/img/*", files)
    extra_datas = []

    for f in files:
        # Note: We trim the destination path down to Odemis' root
        extra_datas.append((f[f.find('odemis'):], f, 'DATA'))

    return extra_datas


# Check what type of viewer we are building
if os.environ.get('FLAVOR') == "delphi":
    name = "DelphiViewer"
    script = "delphi_viewer.py"
else:
    name = "OdemisViewer"
    script = "odemis_viewer.py"

# The statements in a spec file create instances of four classes, Analysis, PYZ, EXE and COLLECT.
#
# * A new instance of class Analysis takes a list of script names as input. It analyzes all
#   imports and other dependencies. The resulting object (assigned to a) contains lists of
#   dependencies in class members named:
#
#   - scripts: the python scripts named on the command line;
#   - pure: pure python modules needed by the scripts;
#   - binaries: non-python modules needed by the scripts;
#   - datas: non-binary files included in the app.
# * An instance of class PYZ is a .pyz archive (described under Inspecting Archives below),
#   which contains all the Python modules from a.pure.
# * An instance of EXE is built from the analyzed scripts and the PYZ archive. This object creates
#   the executable file.
# * An instance of COLLECT creates the output folder from all the other parts.

block_cipher = None
use_upx = False

a = Analysis(
    [script],
    pathex=['.'],
    binaries=None,
    datas=None,
    hiddenimports=[
        'cairo',
        'Queue',
        'odemis.acq.align.keypoint',  # Not used in standard, but could be used by plugins
    ] + get_dataio_imports() + get_wx_imports() + get_libtiff_imports(),
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher
)

a.datas += get_gui_img()

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher
)

# For debug version change debug and console values to True. This will result in more
# verbose error messages.

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='%s.exe' % name,
    debug=False,  # True
    strip=False,
    upx=use_upx,
    console=False,  # True
    icon='odemis-viewer.ico'
)

# This a hack because for "some understood" reason the ctypes hook picks libtiff.dll
# (which is not needed) and reports its name in absolute path, which breaks COLLECT.
a.binaries = [b for b in a.binaries if not b[0].endswith("libtiff.dll")]

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    get_lib_tiff(),
    get_cairo_dlls(),
    get_version(),
    [('OdemisViewer.ico', 'odemis-viewer.ico', 'DATA')],
    strip=False,
    upx=use_upx,
    name=name
)
