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
import shutil

from optparse import OptionParser

""" Mageia(+Mandriva) SVN->Git project migrator

    We take projects from SVN, read the spec and convert them to self-building git repositories.
    
    The migrator is, of course, incremental. It saves its state in a Pickle file and then resumes
    from that. This way, we can amend the code in this script, run again and hopefully manage
    to do the migration right.
"""

parser = OptionParser()

# spec sub-path "contrib/mageia/xx.spec", "contrib/mandriva/xx.spec", "contrib/mageia.spec", "contrib/mandriva.spec",
#   "doc/mageia/*.spec", "mageia.spec"


parser.add_option("-R", "--reset",
                  action="store_true", dest="reset", default=False,
                  help="Forget about pending migrations, start all over.")

parser.add_option("--show",
                  action="store_true", dest="show_mode", default=False,
                  help="Don't do anything, just list the steps")

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
    pgroup1.add_option("--skip", "--skip-steps", dest="skip_steps", type=int,
                        help="Number of steps to skip")
    pgroup1.add_option("--one-step", action='store_true', default=False,
                        help="Perform one step and then stop")
    pgroup1.add_option("--edit-spec", action='store_true', default=False,
                        help="Stop before parsing the SPEC file for manual edit")
    parser.add_option_group(pgroup1)

    pgroup3 = optparse.OptionGroup(parser, 'SVN repository options')
    pgroup3.add_option("--mga-repo-url", help="Mageia SVN repository URL")
    pgroup3.add_option("--mga-mirror-url", help="Mageia read-only SVN repository URL for checking-out packages")
    pgroup3.add_option("--mga-trunk-dir", help="Mageia SVN trunk-directory, aka. distro version")

    parser.add_option_group(pgroup3)

    pgroup2 = optparse.OptionGroup(parser, 'Config-File options',
                    " These options help run this script with pre-configured settings.")

    pgroup2.add_option("-c", "--config", dest="configfile",
                help="Read configuration options for this script from file. ")
    pgroup2.add_option("--no-config", dest="have_config", action="store_false", default=True,
                help="Do not read the default config file, start with empty options.")

    parser.add_option_group(pgroup2)

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

RPM_SECTIONS = ['description', 'package', 'prep', 'build', 'install', 'clean', 'files', 'changelog']

class SpecContents(object):
    """Contains an RPM spec file
    
        From this data structure, the same, exact, spec file should be reconstructed
    """
    _section_re = re.compile('^%('+ '|'.join(RPM_SECTIONS) + r')\b')
    _define_re = re.compile(r'^\s*%define\s+(\w+)\s+(.*)$')
    _header_re = re.compile(r'^(\w+)\s*:\s+(.*)$')
    _varre = re.compile(r'%(?:(?:\{(\w+)\})|(\w+))')
    _condre = re.compile(r'%\{\?(\!?\w+):(.+)\}')

    _vars_to_resolve = ['version', 'release', 'name']
    _vars_to_skip = ['version', 'release', 'name']

    _setup_re = re.compile(r'^\s*%setup\s+(.*)$')
    _autosetup_re = re.compile(r'^\s*%autosetup(\s+.*)$')
    _autopatch_re = re.compile(r'^\s*%autopatch(\s+.*)$')
    _patch_re = re.compile(r'^\s*%patch([0-9]+)\s+(.*)$')
    _apply_patches_re = re.compile(r'^\s*%apply_patches\s*$')

    def __init__(self):
        self.sections = {}
        self.section_heads = {'': None }
        self.variables = {'nil': ''} # detected variable substitutions from %define
        self.spec_vars = {}
        self.section_order = ['',]
        self._sources = {}
        self._patches = {}
        self._prep_steps = []
        self._prev_comments = []
        self._patch_comments = {}
        self._source_comments = {}
        self._svndir = False
        self._used_sources = {}

    def replace_vars(self, varstr):
        """return the string with %define'd variables replaced inline

            Note: it does NOT strip the resulting string, will preserve whitespace
        """
        while True:
            oldstr = varstr
            varstr = self._condre.sub(self._resolve_cond, varstr)
            if oldstr != varstr:
                continue
            varstr = self._varre.sub(self._resolve, varstr)
            if oldstr == varstr:
                break
        return varstr

    def _resolve_sources(self, varm):
        orig = varm.group(1) or varm.group(2)
        if not orig.startswith('SOURCE'):
            return varm.group(0)
        sno = orig[6:]
        if sno not in self._sources:
            for key in self._sources.keys():
                if int(key) == int(sno):
                    sno = key
                    break
            else:
                raise KeyError("SOURCE%s not found in declared sources" % sno)
        if sno not in self._used_sources:
            src = os.path.basename(self._sources[sno])
            self._used_sources[sno] = os.path.join('contrib', 'mageia', src)
            self._prep_steps.append((Copy_Source, dict(source=src, dest=self._used_sources[sno])))
            self._prep_steps.append((Git_Commit_Source, dict(msg=self._source_comments.get(sno, \
                    "source from Mageia: %s" % sno) )))

        return self._used_sources[sno]

    def _resolve(self, varm):
        orig = varm.group(0)
        var = varm.group(1) or varm.group(2)
        new = self._resolve_sources(varm)
        if new != orig:
            return new

        r = self.variables.get(var, None)
        if r is None:
            return orig
        elif r is True:
            return '1'
        elif r:
            return r
        else:
            return orig

    def _resolve_cond(self, varm):
        """Resolve RPM conditional, like %{?group1:group2}
        """
        cond = varm.group(1)
        inverse = False
        if cond.startswith('!'):
            inverse = True
            cond = cond[1:]

        r = self.variables.get(cond, None)
        if inverse:
            r = not r
        if r:
            return varm.group(2)
        else:
            return ''

    def parse_in(self, fp, mstep):
        seclist, line_fn = self._init_section_('')
        self._svndir = mstep._parent._svndir
        
        for line in fp:
            if not line:
                continue

            ms = self._section_re.match(line)
            if ms:
                cur_section = ms.group(1)
                rest = line[ms.end():].strip()
                init_fn = getattr(self, '_init_section_%s' % cur_section)
                if cur_section in ('description', 'package', 'files', 'pre_un' ) and rest: # TODO
                    cur_section = (cur_section, rest)
                    rest = None
                _logger.debug("Parsing section: %s", cur_section)
                if cur_section in self.sections:
                    _logger.warning("Section defined twice: %s", cur_section)
                self.section_order.append(cur_section)
                seclist, line_fn = init_fn(cur_section, rest)
                continue

            line_fn(line, seclist)

    def gitify_out(self, fp):

        for section in self.section_order:
            section_lines = self.sections[section]
            if section:
                package = None
                if isinstance(section, tuple):
                    section, package = section
                ss = '%' + section
                if section in self.section_heads:
                    ss += ' ' + self.section_heads[section]
                if package:
                    ss += ' ' + package
                ss += '\n'
                fp.write(ss)

            for line in section_lines:
                fp.write(line)

    def _init_section_(self, section_id, rest=''):
        return self.sections.setdefault(section_id, []), self._proc_line_default
        
        #elif cur_section == '' and ':' in line:
        #        section.append(line)
    
    def _proc_line_default(self, line, section):
        """Process lines of default (header) section
        """
        dmp = self._define_re.match(line)
        hmp = None
        if dmp:

            var = dmp.group(1)
            value = dmp.group(2)
            if var in self.variables:
                _logger.warning("Variable '%s' %%define'd twice!", var)
            if var in self._vars_to_resolve:
                value = self.replace_vars(value)

            self.variables[var] = value

            if var in self._vars_to_skip:
                return
        elif line.startswith('#Patch') or line.startswith('# Patch'):
            # Must be some commented-out patch, reset comments
            self._prev_comments = []
        elif line[0] == '#':
            self._prev_comments.append(line[1:].lstrip())
        else:
            hmp = self._header_re.match(line)
            if hmp:
                hvar = hmp.group(1)
                hvalue = hmp.group(2)
                hdr_comments = self._prev_comments
                self._prev_comments = []
                if hvar == 'Name':
                    new_val = self.replace_vars(hvalue)
                    section.append('Name:\t\t%s\n' % new_val)
                    if 'name' not in self.variables:
                        self.variables['name'] = new_val
                    return
                elif hvar == 'Version':
                    self.spec_vars['version'] = self.replace_vars(hvalue).strip()
                    self.variables['version'] = self.spec_vars['version'] # because rpm does that, too
                    section.append('Version:\t%git_get_ver\n')
                    _logger.debug("Found upstream version: %s", self.spec_vars['version'])
                    return
                elif hvar == 'Release':
                    self.spec_vars['release'] = self.replace_vars(hvalue).strip()
                    section.append('Release:\t%mkrel %git_get_rel2\n')
                    _logger.debug("Found upstream release: %s", self.spec_vars["release"])
                    return
                elif hvar.startswith('Source'):
                    # gitify
                    src_num = hvar[6:]
                    if not self._sources:
                        section.append('Source:\t\t%git_bs_source %{name}-%{version}.tar.gz\n')
                        section.append('Source1:\t%{name}-gitrpm.version\n')
                        section.append('Source2:\t%{name}-changelog.gitrpm.txt\n')
                    self._sources[src_num] = self.replace_vars(hvalue).strip().rsplit('/', 1)[-1]
                    if hdr_comments:
                        self._source_comments[src_num] = hdr_comments
                    return
                elif hvar.startswith('Patch'):
                    patch_num = int(hvar[5:].strip())
                    self._patches[patch_num] = os.path.basename(self.replace_vars(hvalue).strip())
                    if hdr_comments:
                        self._patch_comments[patch_num] = hdr_comments
                    return

        section.append(line)

    def _proc_line_plain(self, line, section):
        """Just append lines to the section buffer
        """
        section.append(line)

    def _proc_line_sources(self, line, section):
        """ Replace "sources" variables, then append to section buffer
        """
        line = self._varre.sub(self._resolve_sources, line)
        section.append(line)

    def _init_section_package(self, section_id, rest):
        # note TODO we only do plain line processing, not variable substitution!
        return self.sections.setdefault(section_id, []), self._proc_line_plain
        
    def _init_section_description(self, section_id, rest):
        assert not rest, "description: %s" % rest
        #if rest:
        #    self.section_heads[cur_section] = rest
        
        return self.sections.setdefault(section_id, []), self._proc_line_plain
    
    def _proc_line_prep(self, line, seclines):
        """Process lines for the 'prep' section
        
            Much to do here. We clean the '%setup' directives and parse the Sources
            needed. Then, prepare the list of patches to apply
        """
        
        smp = self._setup_re.match(line)
        if smp:
            args = self.replace_vars(smp.group(1)).strip().split()
            # print "args:", args
            name = None
            source = self._sources.get('', None)
            if source is None:
                source = self._sources.get('0', None)
            while args:
                r0 = args.pop(0)
                if not r0:
                    continue
                elif r0 == '-q':
                    pass
                elif r0 == '-n':
                    name = args.pop(0)
                else:
                    _logger.warning("Unknown switch to %%setup: '%s'", r0)
            assert source, "No source to extract! %r" % self._sources.keys()
            _logger.debug("Will extract source from %s, name=%s", source, name)
            self._prep_steps.append((Untar, dict(source=source, pname=name)))
            if len(self._prep_steps) == 1:
                self._prep_steps.append((Git_Commit_Source, dict(msg="Initial source from Mageia %s" % source)))
                self._prep_steps.append((Git_tag, dict(tag='v'+self.spec_vars.get('version', '0.0'))))
                self._prep_steps.append((Placeholder, {}))
                seclines.append('%git_get_source\n')
                seclines.append('%setup -q\n')
            else:
                self._prep_steps.append((Git_Commit_Source, dict(msg="Add source from %s" % source)))
            return

        pmp = self._patch_re.match(line)
        if pmp:
            args = self.replace_vars(pmp.group(2)).strip().split()
            patch_level = 0
            while args:
                r0 = args.pop(0)
                if not r0:
                    continue
                elif r0 == '-q':
                    pass
                elif r0[:2] == '-p':
                    if len(r0) > 2:
                        patch_level = int(r0[2:])
                    else:
                        patch_level = int(args.pop(0))
                elif r0 == '-b':
                    args.pop(0)
                else:
                    _logger.warning("Unknown switch to %%patch: '%s'", r0)
            patch_num = int(pmp.group(1))
            self._prep_patch(patch_num, line=line, patch_level=patch_level)
            return

        if self._apply_patches_re.match(line):
            _logger.debug("Apply all patches: %r", self._patches.keys())
            for pnum in sorted(self._patches.keys()):
                self._prep_patch(pnum, line=line)
            return

        smp2 = self._autosetup_re.match(line)
        if smp2:
            args = self.replace_vars(smp2.group(1)).strip().split()
            # print "args:", args
            name = None
            source = self._sources.get('', None)
            if source is None:
                source = self._sources.get('0', None)
            while args:
                r0 = args.pop(0)
                if not r0:
                    continue
                elif r0 == '-q':
                    pass
                elif r0 == '-n':
                    name = args.pop(0)
                elif r0 == '-S':
                    vcs = args.pop(0)
                    if vcs != 'git':
                        _logger.warning("Found %%autosetup -S %s , which may fail", vcs)
                else:
                    _logger.warning("Unknown switch to %%autosetup: '%s'", r0)
            assert source, "No source to extract! %r" % self._sources.keys()
            _logger.debug("Will extract source from %s, name=%s", source, name)
            self._prep_steps.append((Untar, dict(source=source, pname=name)))
            if len(self._prep_steps) == 1:
                self._prep_steps.append((Git_Commit_Source, dict(msg="Initial source from Mageia %s" % source)))
                self._prep_steps.append((Git_tag, dict(tag='v'+self.spec_vars.get('version', '0.0'))))
                self._prep_steps.append((Placeholder, {}))
                seclines.append('%git_get_source\n')
                seclines.append('%setup -q\n')
            else:
                self._prep_steps.append((Git_Commit_Source, dict(msg="Add source from %s" % source)))
            _logger.debug("Apply all patches: %r", self._patches.keys())
            for pnum in sorted(self._patches.keys()):
                self._prep_patch(pnum, line=line)
            return

        smp2 = self._autopatch_re.match(line)
        if smp2:
            args = self.replace_vars(smp2.group(1)).strip().split()
            # print "args:", args
            patch_level = 1
            while args:
                r0 = args.pop(0)
                if not r0:
                    continue
                elif r0 == '-p':
                    patch_level = int(args.pop(0))
                elif r0.startswith('-p'):
                    patch_level = int(r0[2:])
                else:
                    _logger.warning("Unknown switch to %%autosetup: '%s'", r0)
                    raise NotImplementedError(line)
            _logger.debug("Apply all patches: %r", self._patches.keys())
            for pnum in sorted(self._patches.keys()):
                self._prep_patch(pnum, line=line, patch_level=patch_level)
            return

        if line.strip().startswith('%'):
            if line.startswith(('%if', '%else', '%endif')):
                # TODO: nested %if processing
                pass
            elif line.startswith('%define'):
                pass
            else:
                _logger.warning("Unknown line in setup: %s", line.strip())
                raise NotImplementedError(line)

        line = self._varre.sub(self._resolve_sources, line)
        seclines.append(line)

    def _prep_patch(self, patch_num, line='', patch_level=1):
        """Push patch in our _prep_steps
        
            Will auto-detect git patches and 'am' them instead
        """
        if patch_num not in self._patches:
            _logger.error("Patch %s not found for line: %s", patch_num, line.strip())
            raise RuntimeError("Patch not found")

        if patch_level != 1:
            raise NotImplementedError("patch level %s" % patch_level)

        # detect git patches:
        git_from_re = re.compile(r'From ([0-9a-f]{40}) ')
        git_log_re = re.compile(r'commit ([0-9a-f]{40})')
        try:
            fp = None
            patch_path = self._patches[patch_num]
            if '/' in patch_path:
                patch_path = patch_path.rsplit('/',1)[1]
            patch_fullpath = os.path.join(self._svndir, 'SOURCES', patch_path)
            fp = open(patch_fullpath, 'rb')
            first_line = fp.readline()
            
            if git_log_re.match(first_line):
                # it comes from 'git log --patch-with-stat' , which must be translated to 'git am'
                _logger.debug("Patch %s seems to come from git, using 'git am'", patch_path)
                self._prep_steps.append((Git_Log_Patch, dict(source=patch_path)))
                return
            elif git_from_re.match(first_line):
                # It is a git patch
                _logger.debug("Patch %s seems to come from git, using 'git am'", patch_path)
                self._prep_steps.append((Git_Am_Patch, dict(source=patch_path)))
                return
            elif first_line.startswith(('diff ', '---', 'Index:')):
                # regular diff patch
                pass
            else:
                _logger.info('Patch "%s" starts with: %s', patch_path, first_line[:60])
                if not self._patch_comments.get(patch_num, None):
                    # try to read first lines of patch as comment
                    n = 1
                    comment_lines = []
                    while n < 100:
                        comment_lines.append(first_line.rstrip('\n'))
                        first_line = fp.readline()
                        if first_line.startswith(('diff ', '--- ')):
                            break
                        n += 1
                    comment_lines += ["", "patch: %s from upstream" % self._patches[patch_num]]
                    self._patch_comments[patch_num] = comment_lines

        except Exception:
            _logger.warning("Cannot auto-detect patch: %s", patch_path, exc_info=True)
        finally:
            if fp:
                fp.close()

        # regular patch:
        self._prep_steps.append((Patch, dict(source=patch_path, patch_level=patch_level)))
        self._prep_steps.append((Git_Commit_Source, dict(msg=self._patch_comments.get(patch_num, '') \
                    or "apply patch: %s" % patch_path) ))

    def _init_section_prep(self, section_id, rest):
        assert not rest, "prep: %s" % rest
        return self.sections.setdefault(section_id, []), self._proc_line_prep

    def _init_section_build(self, section_id, rest):
        assert not rest, "build: %s" % rest
        return self.sections.setdefault(section_id, []), self._proc_line_sources

    def _init_section_install(self, section_id, rest):
        assert not rest, "install: %s" % rest
        return self.sections.setdefault(section_id, []), self._proc_line_sources

    def _init_section_files(self, section_id, rest):
        assert not rest, "files: %s" % rest
        return self.sections.setdefault(section_id, []), self._proc_line_plain

    def _init_section_changelog(self, section_id, rest):
        assert not rest, "changelog: %s" % rest
        return self.sections.setdefault(section_id, []), self._proc_line_plain

    def _init_section_clean(self, section_id, rest):
        assert not rest, "clean: %s" % rest
        return self.sections.setdefault(section_id, []), self._proc_line_plain

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
        if self._parent._svndir:
            _logger.debug("Examining existing svndir: %s", self._parent._svndir)
            if os.path.isdir(os.path.join(self._parent._svndir, self._parent._project, '.svn')):
                _logger.info("Re-using SVN data from %s", self._parent._svndir)
            else:
                _logger.error('Path %s/%s/.svn does not exist', self._parent._svndir, self._parent._project)
                raise Exception("Previous SVN directory not found")
        else:
            self._parent._svndir = tempfile.mkdtemp( prefix="mga_migr_%s_" % self._parent._project, dir=hometmp)
        self._parent._gitdir = tempfile.mkdtemp( prefix="mga_migr_%s_" % self._parent._project, dir=hometmp)
        _logger.info("Temporary path for SVN will be: %s", self._parent._svndir)
        _logger.info("Temporary path for GIT will be: %s", self._parent._gitdir)

class Checkout(MWorker):
    _name = "checkout from SVN"
    def __init__(self, parent):
        super(Checkout, self).__init__(parent)
        global opts
        self._mga_repo = opts.mga_repo_url
        self._mga_mirror = opts.mga_mirror_url
        self._mga_trunk = opts.mga_trunk_dir

    def work(self):
        if os.path.isdir(os.path.join(self._parent._svndir, self._parent._project, '.svn')):
            _logger.info("SVN directory is already here, NOT checking out")
            return
        global opts
        name = self._parent._project
        if self._mga_trunk:
            name = '%s/%s' % ( self._mga_trunk, name)
        if self._mga_mirror:
            name = self._mga_mirror + '/' + name
        elif self._mga_repo:
            name = self._mga_repo + '/' + name
        subprocess.check_call( ['mgarepo', 'co', name ], cwd=self._parent._svndir)
        _logger.debug('Checked out %s to %s .', self._parent._project, self._parent._svndir)
        self._parent._svndir = os.path.join(self._parent._svndir, self._parent._project)

class Set_SVNdir(MWorker):
    _name = "set SVN project dir"

    def work(self):
        if not os.path.isdir(os.path.join(self._parent._svndir, self._parent._project, '.svn')):
            _logger.error("SVN directory is missing, NOT checking out")
            raise Exception("missing svn dir")
        self._parent._svndir = os.path.join(self._parent._svndir, self._parent._project)

class Git_Init(MWorker):
    _name = "initialize git dir"
    def work(self):
        subprocess.check_call(['git', 'init', self._parent._project], cwd=self._parent._gitdir)
        self._parent._gitdir = os.path.join(self._parent._gitdir, self._parent._project)

class Parse_Spec(MWorker):
    """
        After parsing the SPEC file, this worker will *add* the next steps, according
        to the instructions in the spec.
    """
    _name = "parse the spec file"
    
    def work(self):
        fp = open(os.path.join(self._parent._svndir, 'SPECS', self._parent._project + '.spec'), 'rb')
        spec = self._parent._spec = SpecContents()
        spec.parse_in(fp, self)
        
        # Now, replace the two placeholders in migrator's chain with the prep steps
        for t in 1,2 :
            for i, step in enumerate(self._parent._steps):
                if isinstance(step, Placeholder):
                    self._parent._steps.pop(i)
                    while spec._prep_steps:
                        nclass, kwargs = spec._prep_steps.pop(0)
                        if nclass == Placeholder:
                            break
                        self._parent._steps.insert(i, nclass(self._parent, **kwargs))
                        i += 1
                    break
        _logger.debug("Done parsing the SPEC")

class Untar(MWorker):
    _name = "extract the initial source"
    
    def __init__(self, parent, source, pname):
        """Untar `source` at path name `pname`
        """
        super(Untar, self).__init__(parent)
        self.source = source
        self.pname = pname

    def work(self):
        subprocess.check_call(['tar', 'xf', os.path.join(self._parent._svndir, 'SOURCES', self.source),
                    '--strip-components=1'], cwd=self._parent._gitdir)
        _logger.debug('Extracted source from tar %s', self.source)

class Git_Commit_Source(MWorker):
    _name = "commit the upstream source in git"
    
    def __init__(self, parent, msg):
        """ Commit everything, with message `msg`
        """
        super(Git_Commit_Source, self).__init__(parent)
        self._msg = msg

    def work(self):
        if isinstance(self._msg, list):
            self._msg = '\n'.join(self._msg)
        elif not self._msg:
            self._msg = "Apply patch"
        subprocess.check_call(['git', 'add', '--all'], cwd=self._parent._gitdir)

        # check for untracked files. This may mean .gitignore is too greedy
        # It would be a mistake for the .tar (or patches) to contain files
        # that are not commited in git. They would be lost.
        untracked = subprocess.check_output(['git', 'ls-files', '-o'],
                                            cwd=self._parent._gitdir)
        untracked = filter(None, untracked.split('\n'))

        # Then, try to commit anyway..
        subprocess.check_call(['git', 'commit', '--no-verify', '-m', self._msg], cwd=self._parent._gitdir)

        if untracked:  # .. and stop if there is untracked files
            _logger.error("Untracked files are still in the source repo:%s",
                            ''.join(['\n\t' + s for s in untracked[:10]]))
            _logger.info("Please adjust your .gitignore and manually add a commit here")
            return 'break'


class Git_Mga_branch(MWorker):
    _name = "create a 'mageia' branch"
    
    def __init__(self, parent, branch='mageia'):
        super(Git_Mga_branch, self).__init__(parent)
        self._branch = branch

    def work(self):
        subprocess.check_call(['git', 'checkout', '-b', self._branch], cwd=self._parent._gitdir)

class Chose_Spec_Path(MWorker):
    _name = "find the right place for the spec file"
    
    def work(self):
        self._parent._spec_path = os.path.join('contrib', 'mageia', self._parent._project+'.spec')

class Copy_Spec(MWorker):
    _name = "copy the spec into the git repo"

    def work(self):
        spec_dir = os.path.join(self._parent._gitdir, os.path.dirname(self._parent._spec_path))
        if not os.path.isdir(spec_dir):
            os.makedirs(spec_dir)
        shutil.copy(os.path.join(self._parent._svndir, 'SPECS', self._parent._project + '.spec'),
                os.path.join(self._parent._gitdir, self._parent._spec_path))

class Copy_Source(MWorker):
    _name = "copy source script into git"
    
    def __init__(self, parent, source, dest):
        """ Copy SOURCES/`source` into git/`dest`
        """
        super(Copy_Source, self).__init__(parent)
        assert source
        assert dest
        self.source = source
        self.dest = dest

    def work(self):
        if '/' in self.source:
            self.source = os.path.basename(self.source)
        _logger.info("Copying source: %s", self.source)
        shutil.copy(os.path.join(self._parent._svndir, 'SOURCES', self.source),
                    os.path.join(self._parent._gitdir, self.dest))
    
class Git_Commit_Spec(MWorker):
    _name = "commit the spec file in git"

    def work(self):
        subprocess.check_call(['git', 'add', self._parent._spec_path], cwd=self._parent._gitdir)
        subprocess.check_call(['git', 'commit', '-m', 'mga: SPEC file, from Mageia',],
                cwd=self._parent._gitdir)

class Placeholder(MWorker):
    _name = "no op placeholder"

    def work(self):
        raise RuntimeError("A placeholder cannot ever work. You forgot to replace it.")

class Edit_spec(MWorker):
    _name = "edit the spec file"

    def work(self):
        return 'break'

class Gitify_Spec(MWorker):
    _name = "gitify the spec file"
    
    def work(self):
        fp = open(os.path.join(self._parent._gitdir, self._parent._spec_path), 'wb')
        fp.write('%%define git_repo %s\n%%define git_head HEAD\n\n' % self._parent._project)
        self._parent._spec.gitify_out(fp)
        fp.close()

class Git_Commit_Spec2(MWorker):
    _name = "commit the gitified spec"

    def work(self):
        subprocess.check_call(['git', 'add', self._parent._spec_path], cwd=self._parent._gitdir)
        subprocess.check_call(['git', 'commit', '-m', 'mga: gitify the spec file',],
                cwd=self._parent._gitdir)

class Git_tag(MWorker):
    _name = "add the versioned tag to git"
    
    def __init__(self, parent, tag):
        super(Git_tag, self).__init__(parent)
        self._tag = tag

    def work(self):
        subprocess.check_call(['git', 'tag', self._tag], cwd=self._parent._gitdir)

class Patch(MWorker):
    _name = "patch"
    
    def __init__(self, parent, source, patch_level=0):
        """Apply patch from `source`
        """
        super(Patch, self).__init__(parent)
        self.source = source
        self.patch_level = patch_level

    def work(self):
        if '/' in self.source:
            self.source = os.path.basename(self.source)
        _logger.info("Applying patch: %s", self.source)
        subprocess.check_call(['patch', '-p%d' % self.patch_level, '-F0', '--no-backup-if-mismatch', '-i',
                    os.path.join(self._parent._svndir, 'SOURCES', self.source)],
                cwd=self._parent._gitdir)

class Git_Am_Patch(MWorker):
    _name = "git am"

    def __init__(self, parent, source):
        """Apply git mailbox patch
          
          see: `git am`
        """
        super(Git_Am_Patch, self).__init__(parent)
        self.source = source

    def work(self):
        _logger.info("Applying mailbox patch: %s in %s", self.source, self._parent._gitdir)
        subprocess.check_call(['git', 'am', os.path.join(self._parent._svndir, 'SOURCES', self.source)],
                cwd=self._parent._gitdir)

class Git_Log_Patch(MWorker):
    _name = "git apply patch"

    def __init__(self, parent, source):
        """Apply git mailbox patch
          
          see: `git am`
        """
        super(Git_Log_Patch, self).__init__(parent)
        self.source = source

    def work(self):
        _logger.info("Applying log patch: %s in %s", self.source, self._parent._gitdir)
        patch_fullpath = os.path.join(self._parent._svndir, 'SOURCES', self.source)
        try:
            fp = None
            fp = open(patch_fullpath, 'rb')
            if not fp.readline().startswith('commit '):
                raise ValueError("First line does not start with 'commit'")

            author = date = False
            # read first 3 lines: author and date
            for line in fp:
                if not line.strip():
                    break
                if line.startswith('Author: '):
                    assert not author
                    author = line[8:].strip()
                elif line.startswith('Date: '):
                    assert not date
                    date = line[6:].strip()
                else:
                    _logger.warning("Invalid line: %s", line[:60].rstrip())
                    break

            message = ''
            # next lines, the message, all lines have 4 spaces indent
            for line in fp:
                if not line.startswith('    '):
                    break
                message += line[4:]
        finally:
            if fp:
                fp.close()

        subprocess.check_call(['git', 'apply', '--index', patch_fullpath ], cwd=self._parent._gitdir)
        subprocess.check_call(['git', 'commit', '--author', author, '--date', date,
                                '-m', message], cwd=self._parent._gitdir )

class Migrator(object):
    
    def __init__(self, project, old_svndir=None):
        self._project = project
        self._steps = []
        self._svndir = old_svndir
        self._gitdir = None
        self._context = {}
        self._spec = None
        self._spec_path = None
        edit_spec = ()
        global opts
        seq = [Set_Paths,]
        if old_svndir:
            seq.append(Set_SVNdir)
        else:
            seq.append(Checkout)
        seq.append(Git_Init)
        if opts.edit_spec:
            seq.append(Edit_spec)
        seq += [ Parse_Spec, Placeholder, \
                    Git_Mga_branch, Chose_Spec_Path, Copy_Spec, Git_Commit_Spec, \
                    Placeholder, Gitify_Spec, Git_Commit_Spec2 ]
        _logger.debug("sequence of steps for new migrator: %r", seq)
        for sclass in seq:
            assert issubclass(sclass, MWorker), sclass
            self._steps.append(sclass(self))

    def __repr__(self):
        if self._steps:
            return "Migrator<%s>  %s" % (self._project, self._steps[0])

    def finished(self):
        return not self._steps

    def pre_check(self):
        if self._svndir and not os.path.isdir(self._svndir):
                raise EnvironmentError("SVN temporary dir not found: %s" % self._svndir)
        if self._gitdir and not os.path.isdir(self._gitdir):
                raise EnvironmentError("GIT temporary dir not found: %s" % self._gitdir)

    def work(self):
        step = self._steps[0]
        _logger.debug('Trying to %s at migrator of %s', step, self._project)
        r = step.work()
        self._steps.pop(0)
        return r
    
    def skip(self):
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

if copt.show_mode:
    if args:
        _logger.error("Show mode cannot have arguments!")
        sys.exit(1)
    for mig in migs:
        print "Migrator: %s" % mig._project
        print "    remaining steps:"
        for n, s in enumerate(mig._steps):
            print "    %2d. %s" %(n, s)
        print "\nSVN sources path: %s\nGit repo path:    %s" %(mig._svndir, mig._gitdir)
        print

    sys.exit(0)

for project in args:
    try:
        if os.sep in project:
            # must be the SVN dir of some previous migration
            if project.endswith(os.sep):
                project = project[:-1]
            oldsvn1 = project
            oldsvn, project = project.rsplit(os.sep, 1)
            if not os.path.isdir(os.path.join(oldsvn, project, '.svn')):
                raise Exception("Path %s/%s/.svn does not exist" %( oldsvn, project))
            # clean existing migrators for the same dir
            migs = filter(lambda m: (m._svndir != oldsvn) and (m._svndir != oldsvn1), migs)
            migs.append(Migrator(project, oldsvn))
        else:
            migs.append(Migrator(project))
    except Exception:
        _logger.exception("Cannot add migrator for \"%s\" project", project)

if not migs:
    _logger.warning("Must have at least one project to work at. List is empty now!")
    sys.exit(0)

steps_skip = 0
if copt.skip_steps:
    steps_skip = int(copt.skip_steps)

for mig in migs:
    _logger.debug("Using migrator of %s", mig._project)
    try:
        mig.pre_check()
    except EnvironmentError, e:
        _logger.error("Cannot continue migration: %s : %s", mig._project, e)
        continue

    while not mig.finished():
        if steps_skip:
            mig.skip()
            steps_skip -= 1
        try:
            r = mig.work()
            if r == 'break':
                break
        except subprocess.CalledProcessError, e:
            _logger.error("Cannot do %r: %s", mig, e)
            break
        except Exception:
            _logger.exception("Failed migration of %r", mig)
            break
        if copt.one_step:
            break

    if mig.finished():
        _logger.info("Migrator finished, your package is at: %s", mig._gitdir)

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
