import sys
from pathlib import Path
from binascii import hexlify as hx, unhexlify as uhx

sys.path.append('./NSTools/py')
from nstools.Fs import Pfs0, Nca, Type, factory, Nsp
from nstools.lib import FsTools
from nstools.nut import Keys

from titles import *

Keys.load('/home/a1ex/projects/ownfoil/app/config/keys.txt')

f = '/storage/media/games/switch/The Legend of Zelda Breath of the Wild [NSP]/The Legend of Zelda Breath of the Wild [01007EF00011E800][v786432].nsp'
nsp = Nsp.Nsp(f)
nsp.open()
# container.open(filepath, 'rb')
for nspf in nsp:
        if isinstance(nspf, Nca.Nca) and nspf.header.contentType == Type.Content.META:
            print(nspf.header.contentType)
            for section in nspf:
                if isinstance(section, Pfs0.Pfs0):
                    Cnmt = section.getCnmt()
# p = Path(f).resolve()
# container = factory(Path(f).resolve())
# container.open(f, 'rb')