#!/usr/bin/python

from ConfigParser import SafeConfigParser, NoSectionError
import optparse
import sys, os
import logging
import subprocess
import re
import cPickle as pickle
import errno
import tempfile

from optparse import OptionParser

parser = OptionParser()

# spec sub-path "contrib/mageia/xx.spec", "contrib/mandriva/xx.spec", "contrib/mageia.spec", "contrib/mandriva.spec",
#   "doc/mageia/*.spec", "mageia.spec"


parser.add_option("-R", "--reset",
                  action="store_true", dest="reset", default=False,
                  help="Forget about pending migrations, start all over.")

# TODO: workdir?
#parser.add_option("-p", "--port", dest="port", default=8000,
                  #help="bind to PORT", metavar="PORT")


_non_options = ['configfile', 'config_section', 'have_config',]
_list_options = {} #: Options that must be parsed as a list. optname: key pairs
_path_options = ['homedir', 'logfile',] #: options that must be path-expanded

def _parse_option_section(conf, items, copt, opt):
    """ Parse a .conf file section into `opt`

        @param conf the Config object
        @param items the items section of that config file
        @param copt the optparse options
        @param opt copy of the optparse options, that will receive values
        @param _allow_include levels of recursive include to allow
    """
    global config_stray_opts, _non_options, _list_options, _path_options

    for key, val in items:
        if key in _non_options:
            continue
        elif key in dir(copt):
            if isinstance(getattr(copt, key), list) or \
                    (key in ('modules',)):
                val = val.split(' ')
            elif isinstance(getattr(copt, key), bool):
                val = bool(val.lower() in ('1', 'true', 't', 'yes'))

            if not getattr(copt, key):
                setattr(opt, key, val)

if True:
    pgroup1 = optparse.OptionGroup(parser, 'Standard Options',)
    pgroup1.add_option("-v", "--verbose", "--debug", dest="debug", action='store_true', default=False,
                        help="Enable detailed log messages")
    pgroup1.add_option("--quiet", dest="quiet", action='store_true', default=False,
                        help="Print only error messages")
    pgroup1.add_option("--log", dest="logfile", help="A file to write plain log to, or 'stderr'")

    pgroup2 = optparse.OptionGroup(parser, 'Config-File options',
                    " These options help run this script with pre-configured settings.")

    pgroup2.add_option("-c", "--config", dest="configfile",
                help="Read configuration options for this script from file. ")
    pgroup2.add_option("--no-config", dest="have_config", action="store_false", default=True,
                help="Do not read the default config file, start with empty options.")

(copt, args) = parser.parse_args()

if True:
    conf_read = False
    opts = optparse.Values(copt.__dict__)
    if copt.have_config:
        if copt.configfile:
            cfiles = [copt.configfile,]
        else:
            cfiles = "~/.mga_migrator.conf"

        if cfiles:
            cfiles = map(os.path.expanduser, cfiles)
            cfgparser = SafeConfigParser()
            conf_filesread = cfgparser.read(cfiles)
            try:
                _parse_option_section(cfgparser, cfgparser.items('global'), copt, opts)
                conf_read = True
            except NoSectionError:
                conf_read = False
                pass

    # initialize logging
    log_kwargs = dict(level=logging.INFO)
    if opts.debug:
        log_kwargs['level'] = logging.DEBUG
    elif opts.quiet:
        log_kwargs['level'] = logging.WARN

    if opts.logfile and opts.logfile != 'stderr':
        log_kwargs['filename'] = os.path.expanduser(opts.logfile)
    logging.basicConfig(**log_kwargs)

    _logger = logging.getLogger('main')
    if conf_read:
        _logger.info("Configuration read from %s", ','.join(conf_filesread))
    else:
        if copt.configfile:
            _logger.warning("Configuration could not be read from %s", copt.configfile)

class MWorker(object):
    _name = '<base>'
    def __init__(self, parent):
        self._parent = parent

    def work(self):
        raise NotImplementedError

    def __str__(self):
        return self._name

class Set_Paths(MWorker):
    _name = "set temp path"

    def work(self):
        hometmp = os.path.expanduser('~/tmp')
        self._parent._svndir = tempfile.mkdtemp( prefix="mga_migr_%s_" % self._parent._project, dir=hometmp)
        self._parent._gitdir = tempfile.mkdtemp( prefix="mga_migr_%s_" % self._parent._project, dir=hometmp)
        _logger.debug("Temporary path for SVN will be: %s", self._parent._svndir)
        _logger.debug("Temporary path for SVN will be: %s", self._parent._gitdir)

class Checkout(MWorker):
    _name = "checkout from SVN"
    
    def work(self):
        subprocess.check_call(['mgarepo', 'co', self._parent._project], cwd=self._parent._svndir)
        _logger.debug('Checked out %s to %s .', self._parent._project, self._parent._svndir)

class Git_Init(MWorker):
    _name = "initialize git dir"
    
class Parse_Spec(MWorker):
    """
        After parsing the SPEC file, this worker will *add* the next steps, according
        to the instructions in the spec.
    """
    _name = "parse the spec file"

class Untar(MWorker):
    _name = "extract the initial source"

class Git_Commit_Source(MWorker):
    _name = "commit the upstream source in git"

class Git_Commit_Spec(MWorker):
    _name = "commit the spec file in git"

class Placeholder(MWorker):
    _name = "no op placeholder"

class Gitify(MWorker):
    _name = "gitify the spec file"

class Git_Commit_Spec2(MWorker):
    _name = "commit the gitified spec"


class Migrator(object):
    
    def __init__(self, project, ):
        self._project = project
        self._steps = []
        self._svndir = None
        self._gitdir = None
        self._context = {}
        for sclass in (Set_Paths, Checkout, Git_Init, Parse_Spec):
            assert issubclass(sclass, MWorker), sclass
            self._steps.append(sclass(self))

    def __repr__(self):
        if self._steps:
            return "Migrator<%s>  %s" % (self._project, self._steps[0])

    def finished(self):
        return not self._steps

    def work(self):
        step = self._steps[0]
        _logger.debug('Trying to %s at migrator of %s', step, self._project)
        step.work()
        self._steps.pop(0)

migs = []

if not copt.reset:
    try:
        ppfile = "~/.mga_migrator.dat"
        fp = open(os.path.expanduser(ppfile), 'rb')
        migs = pickle.load(fp)
        fp.close()
        _logger.info("Loaded %d previous builds from %s", len(migs), ppfile)
    except IOError, e:
        if e.errno == errno.ENOENT:
            pass
        else:
            _logger.exception("Cannot load pickle file:")
    except Exception:
        _logger.exception("Cannot load previous data: ")


for project in args:
    try:
        migs.append(Migrator(project))
    except Exception:
        _logger.exception("Cannot add migrator for \"%s\" project", project)

if not migs:
    _logger.warning("Must have at least one project to work at. List is empty now!")
    sys.exit(0)

for mig in migs:
    _logger.debug("Using migrator of %s", mig._project)
    while not mig.finished():
        try:
            mig.work()
        except subprocess.CalledProcessError, e:
            _logger.error("Cannot do %r: %s", mig, e)
            break
        except Exception:
            _logger.exception("Failed migration of %r", mig)
            break

try:
    nmigs = filter(lambda m: not m.finished(), migs)
    ppfile = "~/.mga_migrator.dat"
    fp = open(os.path.expanduser(ppfile), 'wb')
    pickle.dump(nmigs, fp)
    fp.close()
    _logger.info("Saved %d migrators to %s", len(nmigs), ppfile)
except Exception:
    _logger.exception("Cannot save data: ")

#eof