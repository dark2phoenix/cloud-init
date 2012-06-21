# vi: ts=4 expandtab
#
#    Copyright (C) 2012 Canonical Ltd.
#    Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
#    Copyright (C) 2012 Yahoo! Inc.
#
#    Author: Scott Moser <scott.moser@canonical.com>
#    Author: Juerg Haefliger <juerg.haefliger@hp.com>
#    Author: Joshua Harlow <harlowja@yahoo-inc.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License version 3, as
#    published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

from StringIO import StringIO

import copy as obj_copy
import contextlib
import errno
import glob
import grp
import gzip
import hashlib
import os
import platform
import pwd
import random
import shutil
import socket
import string  # pylint: disable=W0402
import subprocess
import sys
import tempfile
import time
import types
import urlparse

import yaml

from cloudinit import log as logging
from cloudinit import url_helper as uhelp

from cloudinit.settings import (CFG_BUILTIN, CLOUD_CONFIG)


try:
    import selinux
    HAVE_LIBSELINUX = True
except ImportError:
    HAVE_LIBSELINUX = False


LOG = logging.getLogger(__name__)

# Helps cleanup filenames to ensure they aren't FS incompatible
FN_REPLACEMENTS = {
    os.sep: '_',
}
FN_ALLOWED = ('_-.()' + string.digits + string.ascii_letters)

# Helper utils to see if running in a container
CONTAINER_TESTS = ['running-in-container', 'lxc-is-container']


class ProcessExecutionError(IOError):

    MESSAGE_TMPL = ('%(description)s\n'
                    'Command: %(cmd)s\n'
                    'Exit code: %(exit_code)s\n'
                    'Reason: %(reason)s\n'
                    'Stdout: %(stdout)r\n'
                    'Stderr: %(stderr)r')

    def __init__(self, stdout=None, stderr=None,
                 exit_code=None, cmd=None,
                 description=None, reason=None):
        if not cmd:
            self.cmd = '-'
        else:
            self.cmd = cmd

        if not description:
            self.description = 'Unexpected error while running command.'
        else:
            self.description = description

        if not isinstance(exit_code, (long, int)):
            self.exit_code = '-'
        else:
            self.exit_code = exit_code

        if not stderr:
            self.stderr = ''
        else:
            self.stderr = stderr

        if not stdout:
            self.stdout = ''
        else:
            self.stdout = stdout

        if reason:
            self.reason = reason
        else:
            self.reason = '-'

        message = self.MESSAGE_TMPL % {
            'description': self.description,
            'cmd': self.cmd,
            'exit_code': self.exit_code,
            'stdout': self.stdout,
            'stderr': self.stderr,
            'reason': self.reason,
        }
        IOError.__init__(self, message)


class SeLinuxGuard(object):
    def __init__(self, path, recursive=False):
        self.path = path
        self.recursive = recursive
        self.enabled = False
        if HAVE_LIBSELINUX and selinux.is_selinux_enabled():
            self.enabled = True

    def __enter__(self):
        # TODO: Should we try to engage selinux here??
        return None

    def __exit__(self, excp_type, excp_value, excp_traceback):
        if self.enabled:
            LOG.debug("Restoring selinux mode for %s (recursive=%s)",
                      self.path, self.recursive)
            selinux.restorecon(self.path, recursive=self.recursive)


class MountFailedError(Exception):
    pass


def SilentTemporaryFile(**kwargs):
    fh = tempfile.NamedTemporaryFile(**kwargs)
    # Replace its unlink with a quiet version
    # that does not raise errors when the
    # file to unlink has been unlinked elsewhere..
    LOG.debug("Created temporary file %s", fh.name)
    fh.unlink = del_file

    # Add a new method that will unlink
    # right 'now' but still lets the exit
    # method attempt to remove it (which will
    # not throw due to our del file being quiet
    # about files that are not there)
    def unlink_now():
        fh.unlink(fh.name)

    setattr(fh, 'unlink_now', unlink_now)
    return fh


def fork_cb(child_cb, *args):
    fid = os.fork()
    if fid == 0:
        try:
            child_cb(*args)
            os._exit(0)  # pylint: disable=W0212
        except:
            logexc(LOG, ("Failed forking and"
                         " calling callback %s"), obj_name(child_cb))
            os._exit(1)  # pylint: disable=W0212
    else:
        LOG.debug("Forked child %s who will run callback %s",
                  fid, obj_name(child_cb))


def is_true_str(val, addons=None):
    check_set = ['true', '1', 'on', 'yes']
    if addons:
        check_set = check_set + addons
    if str(val).lower().strip() in check_set:
        return True
    return False


def is_false_str(val, addons=None):
    check_set = ['off', '0', 'no', 'false']
    if addons:
        check_set = check_set + addons
    if str(val).lower().strip() in check_set:
        return True
    return False


def translate_bool(val, addons=None):
    if not val:
        # This handles empty lists and false and
        # other things that python believes are false
        return False
    # If its already a boolean skip
    if isinstance(val, (bool)):
        return val
    return is_true_str(val, addons)


def rand_str(strlen=32, select_from=None):
    if not select_from:
        select_from = string.letters + string.digits
    return "".join([random.choice(select_from) for _x in range(0, strlen)])


def read_conf(fname):
    try:
        return load_yaml(load_file(fname), default={})
    except IOError as e:
        if e.errno == errno.ENOENT:
            return {}
        else:
            raise


def clean_filename(fn):
    for (k, v) in FN_REPLACEMENTS.iteritems():
        fn = fn.replace(k, v)
    removals = []
    for k in fn:
        if k not in FN_ALLOWED:
            removals.append(k)
    for k in removals:
        fn = fn.replace(k, '')
    fn = fn.strip()
    return fn


def decomp_str(data):
    try:
        buf = StringIO(str(data))
        with contextlib.closing(gzip.GzipFile(None, "rb", 1, buf)) as gh:
            return gh.read()
    except:
        return data


def find_modules(root_dir):
    entries = dict()
    for fname in glob.glob(os.path.join(root_dir, "*.py")):
        if not os.path.isfile(fname):
            continue
        modname = os.path.basename(fname)[0:-3]
        modname = modname.strip()
        if modname and modname.find(".") == -1:
            entries[fname] = modname
    return entries


def is_ipv4(instr):
    """ determine if input string is a ipv4 address. return boolean"""
    toks = instr.split('.')
    if len(toks) != 4:
        return False

    try:
        toks = [x for x in toks if (int(x) < 256 and int(x) > 0)]
    except:
        return False

    return (len(toks) == 4)


def merge_base_cfg(cfgfile, cfg_builtin=None):
    syscfg = read_conf_with_confd(cfgfile)

    kern_contents = read_cc_from_cmdline()
    kerncfg = {}
    if kern_contents:
        kerncfg = load_yaml(kern_contents, default={})

    # Kernel parameters override system config
    if kerncfg:
        combined = mergedict(kerncfg, syscfg)
    else:
        combined = syscfg

    if cfg_builtin:
        # Combined over-ride anything builtin
        fin = mergedict(combined, cfg_builtin)
    else:
        fin = combined

    return fin


def get_cfg_option_bool(yobj, key, default=False):
    if key not in yobj:
        return default
    return translate_bool(yobj[key])


def get_cfg_option_str(yobj, key, default=None):
    if key not in yobj:
        return default
    val = yobj[key]
    if not isinstance(val, (str, basestring)):
        val = str(val)
    return val


def system_info():
    return {
        'platform': platform.platform(),
        'release': platform.release(),
        'python': platform.python_version(),
        'uname': platform.uname(),
    }


def get_cfg_option_list(yobj, key, default=None):
    """
    Gets the C{key} config option from C{yobj} as a list of strings. If the
    key is present as a single string it will be returned as a list with one
    string arg.

    @param yobj: The configuration object.
    @param key: The configuration key to get.
    @param default: The default to return if key is not found.
    @return: The configuration option as a list of strings or default if key
        is not found.
    """
    if not key in yobj:
        return default
    if yobj[key] is None:
        return []
    val = yobj[key]
    if isinstance(val, (list)):
        # Should we ensure they are all strings??
        cval = [str(v) for v in val]
        return cval
    if not isinstance(val, (str, basestring)):
        val = str(val)
    return [val]


# get a cfg entry by its path array
# for f['a']['b']: get_cfg_by_path(mycfg,('a','b'))
def get_cfg_by_path(yobj, keyp, default=None):
    cur = yobj
    for tok in keyp:
        if tok not in cur:
            return default
        cur = cur[tok]
    return cur


def fixup_output(cfg, mode):
    (outfmt, errfmt) = get_output_cfg(cfg, mode)
    redirect_output(outfmt, errfmt)
    return (outfmt, errfmt)


# redirect_output(outfmt, errfmt, orig_out, orig_err)
#  replace orig_out and orig_err with filehandles specified in outfmt or errfmt
#  fmt can be:
#   > FILEPATH
#   >> FILEPATH
#   | program [ arg1 [ arg2 [ ... ] ] ]
#
#   with a '|', arguments are passed to shell, so one level of
#   shell escape is required.
def redirect_output(outfmt, errfmt, o_out=None, o_err=None):
    if not o_out:
        o_out = sys.stdout
    if not o_err:
        o_err = sys.stderr

    if outfmt:
        LOG.debug("Redirecting %s to %s", o_out, outfmt)
        (mode, arg) = outfmt.split(" ", 1)
        if mode == ">" or mode == ">>":
            owith = "ab"
            if mode == ">":
                owith = "wb"
            new_fp = open(arg, owith)
        elif mode == "|":
            proc = subprocess.Popen(arg, shell=True, stdin=subprocess.PIPE)
            new_fp = proc.stdin
        else:
            raise TypeError("Invalid type for output format: %s" % outfmt)

        if o_out:
            os.dup2(new_fp.fileno(), o_out.fileno())

        if errfmt == outfmt:
            LOG.debug("Redirecting %s to %s", o_err, outfmt)
            os.dup2(new_fp.fileno(), o_err.fileno())
            return

    if errfmt:
        LOG.debug("Redirecting %s to %s", o_err, errfmt)
        (mode, arg) = errfmt.split(" ", 1)
        if mode == ">" or mode == ">>":
            owith = "ab"
            if mode == ">":
                owith = "wb"
            new_fp = open(arg, owith)
        elif mode == "|":
            proc = subprocess.Popen(arg, shell=True, stdin=subprocess.PIPE)
            new_fp = proc.stdin
        else:
            raise TypeError("Invalid type for error format: %s" % errfmt)

        if o_err:
            os.dup2(new_fp.fileno(), o_err.fileno())


def make_url(scheme, host, port=None,
                path='', params='', query='', fragment=''):

    pieces = []
    pieces.append(scheme or '')

    netloc = ''
    if host:
        netloc = str(host)

    if port is not None:
        netloc += ":" + "%s" % (port)

    pieces.append(netloc or '')
    pieces.append(path or '')
    pieces.append(params or '')
    pieces.append(query or '')
    pieces.append(fragment or '')

    return urlparse.urlunparse(pieces)


def obj_name(obj):
    if isinstance(obj, (types.TypeType,
                        types.ModuleType,
                        types.FunctionType,
                        types.LambdaType)):
        return str(obj.__name__)
    return obj_name(obj.__class__)


def mergemanydict(srcs, reverse=False):
    if reverse:
        srcs = reversed(srcs)
    m_cfg = {}
    for a_cfg in srcs:
        if a_cfg:
            m_cfg = mergedict(m_cfg, a_cfg)
    return m_cfg


def mergedict(src, cand):
    """
    Merge values from C{cand} into C{src}.
    If C{src} has a key C{cand} will not override.
    Nested dictionaries are merged recursively.
    """
    if isinstance(src, dict) and isinstance(cand, dict):
        for (k, v) in cand.iteritems():
            if k not in src:
                src[k] = v
            else:
                src[k] = mergedict(src[k], v)
    return src


@contextlib.contextmanager
def umask(n_msk):
    old = os.umask(n_msk)
    try:
        yield old
    finally:
        os.umask(old)


@contextlib.contextmanager
def tempdir(**kwargs):
    # This seems like it was only added in python 3.2
    # Make it since its useful...
    # See: http://bugs.python.org/file12970/tempdir.patch
    tdir = tempfile.mkdtemp(**kwargs)
    try:
        yield tdir
    finally:
        del_dir(tdir)


def center(text, fill, max_len):
    return '{0:{fill}{align}{size}}'.format(text, fill=fill,
                                            align="^", size=max_len)


def del_dir(path):
    LOG.debug("Recursively deleting %s", path)
    shutil.rmtree(path)


# get gpg keyid from keyserver
def getkeybyid(keyid, keyserver):
    # TODO fix this...
    shcmd = """
    k=${1} ks=${2};
    exec 2>/dev/null
    [ -n "$k" ] || exit 1;
    armour=$(gpg --list-keys --armour "${k}")
    if [ -z "${armour}" ]; then
       gpg --keyserver ${ks} --recv $k >/dev/null &&
          armour=$(gpg --export --armour "${k}") &&
          gpg --batch --yes --delete-keys "${k}"
    fi
    [ -n "${armour}" ] && echo "${armour}"
    """
    args = ['sh', '-c', shcmd, "export-gpg-keyid", keyid, keyserver]
    (stdout, _stderr) = subp(args)
    return stdout


def runparts(dirp, skip_no_exist=True):
    if skip_no_exist and not os.path.isdir(dirp):
        return

    failed = []
    attempted = []
    for exe_name in sorted(os.listdir(dirp)):
        exe_path = os.path.join(dirp, exe_name)
        if os.path.isfile(exe_path) and os.access(exe_path, os.X_OK):
            attempted.append(exe_path)
            try:
                subp([exe_path])
            except ProcessExecutionError as e:
                logexc(LOG, "Failed running %s [%s]", exe_path, e.exit_code)
                failed.append(e)

    if failed and attempted:
        raise RuntimeError('Runparts: %s failures in %s attempted commands'
                           % (len(failed), len(attempted)))


# read_optional_seed
# returns boolean indicating success or failure (presense of files)
# if files are present, populates 'fill' dictionary with 'user-data' and
# 'meta-data' entries
def read_optional_seed(fill, base="", ext="", timeout=5):
    try:
        (md, ud) = read_seeded(base, ext, timeout)
        fill['user-data'] = ud
        fill['meta-data'] = md
        return True
    except OSError as e:
        if e.errno == errno.ENOENT:
            return False
        raise


def read_file_or_url(url, timeout=5, retries=10, file_retries=0):
    if url.startswith("/"):
        url = "file://%s" % url
    if url.startswith("file://"):
        retries = file_retries
    return uhelp.readurl(url, timeout=timeout, retries=retries)


def load_yaml(blob, default=None, allowed=(dict,)):
    loaded = default
    try:
        blob = str(blob)
        LOG.debug(("Attempting to load yaml from string "
                 "of length %s with allowed root types %s"),
                 len(blob), allowed)
        converted = yaml.load(blob)
        if not isinstance(converted, allowed):
            # Yes this will just be caught, but thats ok for now...
            raise TypeError(("Yaml load allows %s root types,"
                             " but got %s instead") %
                            (allowed, obj_name(converted)))
        loaded = converted
    except (yaml.YAMLError, TypeError, ValueError):
        logexc(LOG, "Failed loading yaml blob")
    return loaded


def read_seeded(base="", ext="", timeout=5, retries=10, file_retries=0):
    if base.startswith("/"):
        base = "file://%s" % base

    # default retries for file is 0. for network is 10
    if base.startswith("file://"):
        retries = file_retries

    if base.find("%s") >= 0:
        ud_url = base % ("user-data" + ext)
        md_url = base % ("meta-data" + ext)
    else:
        ud_url = "%s%s%s" % (base, "user-data", ext)
        md_url = "%s%s%s" % (base, "meta-data", ext)

    md_resp = read_file_or_url(md_url, timeout, retries, file_retries)
    md = None
    if md_resp.ok():
        md_str = str(md_resp)
        md = load_yaml(md_str, default={})

    ud_resp = read_file_or_url(ud_url, timeout, retries, file_retries)
    ud = None
    if ud_resp.ok():
        ud_str = str(ud_resp)
        ud = ud_str

    return (md, ud)


def read_conf_d(confd):
    # get reverse sorted list (later trumps newer)
    confs = sorted(os.listdir(confd), reverse=True)

    # remove anything not ending in '.cfg'
    confs = [f for f in confs if f.endswith(".cfg")]

    # remove anything not a file
    confs = [f for f in confs if os.path.isfile(os.path.join(confd, f))]

    cfg = {}
    for conf in confs:
        cfg = mergedict(cfg, read_conf(os.path.join(confd, conf)))

    return cfg


def read_conf_with_confd(cfgfile):
    cfg = read_conf(cfgfile)

    confd = False
    if "conf_d" in cfg:
        confd = cfg['conf_d']
        if confd:
            if not isinstance(confd, (str, basestring)):
                raise TypeError(("Config file %s contains 'conf_d' "
                                 "with non-string type %s") %
                                 (cfgfile, obj_name(confd)))
            else:
                confd = str(confd).strip()
    elif os.path.isdir("%s.d" % cfgfile):
        confd = "%s.d" % cfgfile

    if not confd or not os.path.isdir(confd):
        return cfg

    cfg = mergedict(read_conf_d(confd), cfg)
    return cfg


def read_cc_from_cmdline(cmdline=None):
    # this should support reading cloud-config information from
    # the kernel command line.  It is intended to support content of the
    # format:
    #  cc: <yaml content here> [end_cc]
    # this would include:
    # cc: ssh_import_id: [smoser, kirkland]\\n
    # cc: ssh_import_id: [smoser, bob]\\nruncmd: [ [ ls, -l ], echo hi ] end_cc
    # cc:ssh_import_id: [smoser] end_cc cc:runcmd: [ [ ls, -l ] ] end_cc
    if cmdline is None:
        cmdline = get_cmdline()

    tag_begin = "cc:"
    tag_end = "end_cc"
    begin_l = len(tag_begin)
    end_l = len(tag_end)
    clen = len(cmdline)
    tokens = []
    begin = cmdline.find(tag_begin)
    while begin >= 0:
        end = cmdline.find(tag_end, begin + begin_l)
        if end < 0:
            end = clen
        tokens.append(cmdline[begin + begin_l:end].lstrip().replace("\\n",
                                                                    "\n"))

        begin = cmdline.find(tag_begin, end + end_l)

    return '\n'.join(tokens)


def dos2unix(contents):
    # find first end of line
    pos = contents.find('\n')
    if pos <= 0 or contents[pos - 1] != '\r':
        return contents
    return contents.replace('\r\n', '\n')


def get_hostname_fqdn(cfg, cloud):
    # return the hostname and fqdn from 'cfg'.  If not found in cfg,
    # then fall back to data from cloud
    if "fqdn" in cfg:
        # user specified a fqdn.  Default hostname then is based off that
        fqdn = cfg['fqdn']
        hostname = get_cfg_option_str(cfg, "hostname", fqdn.split('.')[0])
    else:
        if "hostname" in cfg and cfg['hostname'].find('.') > 0:
            # user specified hostname, and it had '.' in it
            # be nice to them.  set fqdn and hostname from that
            fqdn = cfg['hostname']
            hostname = cfg['hostname'][:fqdn.find('.')]
        else:
            # no fqdn set, get fqdn from cloud.
            # get hostname from cfg if available otherwise cloud
            fqdn = cloud.get_hostname(fqdn=True)
            if "hostname" in cfg:
                hostname = cfg['hostname']
            else:
                hostname = cloud.get_hostname()
    return (hostname, fqdn)


def get_fqdn_from_hosts(hostname, filename="/etc/hosts"):
    """
    For each host a single line should be present with
      the following information:

        IP_address canonical_hostname [aliases...]

      Fields of the entry are separated by any number of  blanks  and/or  tab
      characters.  Text  from	a "#" character until the end of the line is a
      comment, and is ignored.	 Host  names  may  contain  only  alphanumeric
      characters, minus signs ("-"), and periods (".").  They must begin with
      an  alphabetic  character  and  end  with  an  alphanumeric  character.
      Optional aliases provide for name changes, alternate spellings, shorter
      hostnames, or generic hostnames (for example, localhost).
    """
    fqdn = None
    try:
        for line in load_file(filename).splitlines():
            hashpos = line.find("#")
            if hashpos >= 0:
                line = line[0:hashpos]
            line = line.strip()
            if not line:
                continue

            # If there there is less than 3 entries
            # (IP_address, canonical_hostname, alias)
            # then ignore this line
            toks = line.split()
            if len(toks) < 3:
                continue

            if hostname in toks[2:]:
                fqdn = toks[1]
                break
    except IOError:
        pass
    return fqdn


def get_cmdline_url(names=('cloud-config-url', 'url'),
                    starts="#cloud-config", cmdline=None):
    if cmdline is None:
        cmdline = get_cmdline()

    data = keyval_str_to_dict(cmdline)
    url = None
    key = None
    for key in names:
        if key in data:
            url = data[key]
            break

    if not url:
        return (None, None, None)

    resp = uhelp.readurl(url)
    if resp.contents.startswith(starts) and resp.ok():
        return (key, url, str(resp))

    return (key, url, None)


def is_resolvable(name):
    """ determine if a url is resolvable, return a boolean """
    try:
        socket.getaddrinfo(name, None)
        return True
    except socket.gaierror:
        return False


def get_hostname():
    hostname = socket.gethostname()
    return hostname


def is_resolvable_url(url):
    """ determine if this url is resolvable (existing or ip) """
    return (is_resolvable(urlparse.urlparse(url).hostname))


def search_for_mirror(candidates):
    """ Search through a list of mirror urls for one that works """
    for cand in candidates:
        try:
            if is_resolvable_url(cand):
                return cand
        except Exception:
            pass
    return None


def close_stdin():
    """
    reopen stdin as /dev/null so even subprocesses or other os level things get
    /dev/null as input.

    if _CLOUD_INIT_SAVE_STDIN is set in environment to a non empty or '0' value
    then input will not be closed (only useful potentially for debugging).
    """
    if os.environ.get("_CLOUD_INIT_SAVE_STDIN") in ("", "0", 'False'):
        return
    with open(os.devnull) as fp:
        os.dup2(fp.fileno(), sys.stdin.fileno())


def find_devs_with(criteria=None, oformat='device',
                    tag=None, no_cache=False, path=None):
    """
    find devices matching given criteria (via blkid)
    criteria can be *one* of:
      TYPE=<filesystem>
      LABEL=<label>
      UUID=<uuid>
    """
    blk_id_cmd = ['blkid']
    options = []
    if criteria:
        # Search for block devices with tokens named NAME that
        # have the value 'value' and display any devices which are found.
        # Common values for NAME include  TYPE, LABEL, and UUID.
        # If there are no devices specified on the command line,
        # all block devices will be searched; otherwise,
        # only search the devices specified by the user.
        options.append("-t%s" % (criteria))
    if tag:
        # For each (specified) device, show only the tags that match tag.
        options.append("-s%s" % (tag))
    if no_cache:
        # If you want to start with a clean cache
        # (i.e. don't report devices previously scanned
        # but not necessarily available at this time), specify /dev/null.
        options.extend(["-c", "/dev/null"])
    if oformat:
        # Display blkid's output using the specified format.
        # The format parameter may be:
        # full, value, list, device, udev, export
        options.append('-o%s' % (oformat))
    if path:
        options.append(path)
    cmd = blk_id_cmd + options
    (out, _err) = subp(cmd)
    entries = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            entries.append(line)
    return entries


def load_file(fname, read_cb=None, quiet=False):
    LOG.debug("Reading from %s (quiet=%s)", fname, quiet)
    ofh = StringIO()
    try:
        with open(fname, 'rb') as ifh:
            pipe_in_out(ifh, ofh, chunk_cb=read_cb)
    except IOError as e:
        if not quiet:
            raise
        if e.errno != errno.ENOENT:
            raise
    contents = ofh.getvalue()
    LOG.debug("Read %s bytes from %s", len(contents), fname)
    return contents


def get_cmdline():
    if 'DEBUG_PROC_CMDLINE' in os.environ:
        cmdline = os.environ["DEBUG_PROC_CMDLINE"]
    else:
        try:
            cmdline = load_file("/proc/cmdline").strip()
        except:
            cmdline = ""
    return cmdline


def pipe_in_out(in_fh, out_fh, chunk_size=1024, chunk_cb=None):
    bytes_piped = 0
    while True:
        data = in_fh.read(chunk_size)
        if data == '':
            break
        else:
            out_fh.write(data)
            bytes_piped += len(data)
            if chunk_cb:
                chunk_cb(bytes_piped)
    out_fh.flush()
    return bytes_piped


def chownbyid(fname, uid=None, gid=None):
    if uid == None and gid == None:
        return
    LOG.debug("Changing the ownership of %s to %s:%s", fname, uid, gid)
    os.chown(fname, uid, gid)


def chownbyname(fname, user=None, group=None):
    uid = -1
    gid = -1
    if user:
        uid = pwd.getpwnam(user).pw_uid
    if group:
        gid = grp.getgrnam(group).gr_gid
    chownbyid(fname, uid, gid)


# Always returns well formated values
# cfg is expected to have an entry 'output' in it, which is a dictionary
# that includes entries for 'init', 'config', 'final' or 'all'
#   init: /var/log/cloud.out
#   config: [ ">> /var/log/cloud-config.out", /var/log/cloud-config.err ]
#   final:
#     output: "| logger -p"
#     error: "> /dev/null"
# this returns the specific 'mode' entry, cleanly formatted, with value
def get_output_cfg(cfg, mode):
    ret = [None, None]
    if not cfg or not 'output' in cfg:
        return ret

    outcfg = cfg['output']
    if mode in outcfg:
        modecfg = outcfg[mode]
    else:
        if 'all' not in outcfg:
            return ret
        # if there is a 'all' item in the output list
        # then it applies to all users of this (init, config, final)
        modecfg = outcfg['all']

    # if value is a string, it specifies stdout and stderr
    if isinstance(modecfg, str):
        ret = [modecfg, modecfg]

    # if its a list, then we expect (stdout, stderr)
    if isinstance(modecfg, list):
        if len(modecfg) > 0:
            ret[0] = modecfg[0]
        if len(modecfg) > 1:
            ret[1] = modecfg[1]

    # if it is a dictionary, expect 'out' and 'error'
    # items, which indicate out and error
    if isinstance(modecfg, dict):
        if 'output' in modecfg:
            ret[0] = modecfg['output']
        if 'error' in modecfg:
            ret[1] = modecfg['error']

    # if err's entry == "&1", then make it same as stdout
    # as in shell syntax of "echo foo >/dev/null 2>&1"
    if ret[1] == "&1":
        ret[1] = ret[0]

    swlist = [">>", ">", "|"]
    for i in range(len(ret)):
        if not ret[i]:
            continue
        val = ret[i].lstrip()
        found = False
        for s in swlist:
            if val.startswith(s):
                val = "%s %s" % (s, val[len(s):].strip())
                found = True
                break
        if not found:
            # default behavior is append
            val = "%s %s" % (">>", val.strip())
        ret[i] = val

    return ret


def logexc(log, msg, *args):
    # Setting this here allows this to change
    # levels easily (not always error level)
    # or even desirable to have that much junk
    # coming out to a non-debug stream
    if msg:
        log.warn(msg, *args)
    # Debug gets the full trace
    log.debug(msg, exc_info=1, *args)


def hash_blob(blob, routine, mlen=None):
    hasher = hashlib.new(routine)
    hasher.update(blob)
    digest = hasher.hexdigest()
    # Don't get to long now
    if mlen is not None:
        return digest[0:mlen]
    else:
        return digest


def rename(src, dest):
    LOG.debug("Renaming %s to %s", src, dest)
    # TODO use a se guard here??
    os.rename(src, dest)


def ensure_dirs(dirlist, mode=0755):
    for d in dirlist:
        ensure_dir(d, mode)


def read_write_cmdline_url(target_fn):
    if not os.path.exists(target_fn):
        try:
            (key, url, content) = get_cmdline_url()
        except:
            logexc(LOG, "Failed fetching command line url")
            return
        try:
            if key and content:
                write_file(target_fn, content, mode=0600)
                LOG.debug(("Wrote to %s with contents of command line"
                          " url %s (len=%s)"), target_fn, url, len(content))
            elif key and not content:
                LOG.debug(("Command line key %s with url"
                          " %s had no contents"), key, url)
        except:
            logexc(LOG, "Failed writing url content to %s", target_fn)


def yaml_dumps(obj):
    formatted = yaml.dump(obj,
                    line_break="\n",
                    indent=4,
                    explicit_start=True,
                    explicit_end=True,
                    default_flow_style=False,
                    )
    return formatted


def ensure_dir(path, mode=None):
    if not os.path.isdir(path):
        # Make the dir and adjust the mode
        LOG.debug("Ensuring directory exists at path %s", path)
        # TODO: check if guard needed??
        with SeLinuxGuard(path=os.path.dirname(path)):
            os.makedirs(path)
        chmod(path, mode)
    else:
        # Just adjust the mode
        chmod(path, mode)


def get_base_cfg(cfg_path=None, builtin=None):
    if not cfg_path:
        cfg_path = CLOUD_CONFIG
    if not builtin:
        builtin = get_builtin_cfg()
    return merge_base_cfg(cfg_path, builtin)


@contextlib.contextmanager
def unmounter(umount):
    try:
        yield umount
    finally:
        if umount:
            umount_cmd = ["umount", '-l', umount]
            subp(umount_cmd)


def mounts():
    mounted = {}
    try:
        # Go through mounts to see what is already mounted
        mount_locs = load_file("/proc/mounts").splitlines()
        for mpline in mount_locs:
            # Format at: http://linux.die.net/man/5/fstab
            try:
                (dev, mp, fstype, opts, _freq, _passno) = mpline.split()
            except:
                continue
            # If the name of the mount point contains spaces these
            # can be escaped as '\040', so undo that..
            mp = mp.replace("\\040", " ")
            mounted[dev] = {
                'fstype': fstype,
                'mountpoint': mp,
                'opts': opts,
            }
        LOG.debug("Fetched %s mounts from %s", mounted, "/proc/mounts")
    except (IOError, OSError):
        logexc(LOG, "Failed fetching mount points from /proc/mounts")
    return mounted


def mount_cb(device, callback, data=None, rw=False, mtype=None, sync=True):
    """
    Mount the device, call method 'callback' passing the directory
    in which it was mounted, then unmount.  Return whatever 'callback'
    returned.  If data != None, also pass data to callback.
    """
    mounted = mounts()
    with tempdir() as tmpd:
        umount = False
        if device in mounted:
            mountpoint = "%s/" % mounted[device]['mountpoint']
        else:
            try:
                mountcmd = ['mount']
                mountopts = []
                if rw:
                    mountopts.append('rw')
                else:
                    mountopts.append('ro')
                if sync:
                    # This seems like the safe approach to do
                    # (where this is on by default)
                    mountopts.append("sync")
                if mountopts:
                    mountcmd.extend(["-o", ",".join(mountopts)])
                if mtype:
                    mountcmd.extend(['-t', mtype])
                mountcmd.append(device)
                mountcmd.append(tmpd)
                subp(mountcmd)
                umount = tmpd
            except (IOError, OSError) as exc:
                raise MountFailedError(("Failed mounting %s "
                                        "to %s due to: %s") %
                                       (device, tmpd, exc))
            mountpoint = "%s/" % tmpd
        with unmounter(umount):
            if data is None:
                ret = callback(mountpoint)
            else:
                ret = callback(mountpoint, data)
            return ret


def get_builtin_cfg():
    # Deep copy so that others can't modify
    return obj_copy.deepcopy(CFG_BUILTIN)


def sym_link(source, link):
    LOG.debug("Creating symbolic link from %r => %r" % (link, source))
    os.symlink(source, link)


def del_file(path):
    LOG.debug("Attempting to remove %s", path)
    try:
        os.unlink(path)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise e


def copy(src, dest):
    LOG.debug("Copying %s to %s", src, dest)
    shutil.copy(src, dest)


def time_rfc2822():
    try:
        ts = time.strftime("%a, %d %b %Y %H:%M:%S %z", time.gmtime())
    except:
        ts = "??"
    return ts


def uptime():
    uptime_str = '??'
    try:
        contents = load_file("/proc/uptime").strip()
        if contents:
            uptime_str = contents.split()[0]
    except:
        logexc(LOG, "Unable to read uptime from /proc/uptime")
    return uptime_str


def ensure_file(path, mode=0644):
    write_file(path, content='', omode="ab", mode=mode)


def chmod(path, mode):
    real_mode = None
    try:
        real_mode = int(mode)
    except (ValueError, TypeError):
        pass
    if path and real_mode:
        LOG.debug("Adjusting the permissions of %s (perms=%o)",
                 path, real_mode)
        # TODO: check if guard needed??
        with SeLinuxGuard(path=path):
            os.chmod(path, real_mode)


def write_file(filename, content, mode=0644, omode="wb"):
    """
    Writes a file with the given content and sets the file mode as specified.
    Resotres the SELinux context if possible.

    @param filename: The full path of the file to write.
    @param content: The content to write to the file.
    @param mode: The filesystem mode to set on the file.
    @param omode: The open mode used when opening the file (r, rb, a, etc.)
    """
    ensure_dir(os.path.dirname(filename))
    LOG.debug("Writing to %s - %s, %s bytes", filename, omode, len(content))
    # TODO: check if guard needed??
    with SeLinuxGuard(path=filename):
        with open(filename, omode) as fh:
            fh.write(content)
            fh.flush()
    chmod(filename, mode)


def delete_dir_contents(dirname):
    """
    Deletes all contents of a directory without deleting the directory itself.

    @param dirname: The directory whose contents should be deleted.
    """
    for node in os.listdir(dirname):
        node_fullpath = os.path.join(dirname, node)
        if os.path.isdir(node_fullpath):
            del_dir(node_fullpath)
        else:
            del_file(node_fullpath)


def subp(args, data=None, rcs=None, env=None, capture=True, shell=False):
    if rcs is None:
        rcs = [0]
    try:
        LOG.debug(("Running command %s with allowed return codes %s"
                   " (shell=%s, capture=%s)"), args, rcs, shell, capture)
        if not capture:
            stdout = None
            stderr = None
        else:
            stdout = subprocess.PIPE
            stderr = subprocess.PIPE
        stdin = subprocess.PIPE
        sp = subprocess.Popen(args, stdout=stdout,
                        stderr=stderr, stdin=stdin,
                        env=env, shell=shell)
        (out, err) = sp.communicate(data)
    except OSError as e:
        raise ProcessExecutionError(cmd=args, reason=e)
    rc = sp.returncode
    if rc not in rcs:
        raise ProcessExecutionError(stdout=out, stderr=err,
                                    exit_code=rc,
                                    cmd=args)
    # Just ensure blank instead of none?? (iff capturing)
    if not out and capture:
        out = ''
    if not err and capture:
        err = ''
    # Useful to note what happened...
    if capture:
        LOG.debug("Stdout: %s", out)
        LOG.debug("Stderr: %s", err)
    return (out, err)


# shellify, takes a list of commands
#  for each entry in the list
#    if it is an array, shell protect it (with single ticks)
#    if it is a string, do nothing
def shellify(cmdlist, add_header=True):
    content = ''
    if add_header:
        content += "#!/bin/sh\n"
    escaped = "%s%s%s%s" % ("'", '\\', "'", "'")
    for args in cmdlist:
        # if the item is a list, wrap all items in single tick
        # if its not, then just write it directly
        if isinstance(args, list):
            fixed = []
            for f in args:
                fixed.append("'%s'" % (str(f).replace("'", escaped)))
            content = "%s%s\n" % (content, ' '.join(fixed))
        elif isinstance(args, (str, basestring)):
            content = "%s%s\n" % (content, args)
        else:
            raise RuntimeError(("Unable to shellify type %s"
                                " which is not a list or string")
                               % (obj_name(args)))
    LOG.debug("Shellified %s to %s", cmdlist, content)
    return content


def is_container():
    """
    Checks to see if this code running in a container of some sort
    """

    for helper in CONTAINER_TESTS:
        try:
            # try to run a helper program. if it returns true/zero
            # then we're inside a container. otherwise, no
            subp([helper])
            return True
        except (IOError, OSError):
            pass

    # this code is largely from the logic in
    # ubuntu's /etc/init/container-detect.conf
    try:
        # Detect old-style libvirt
        # Detect OpenVZ containers
        pid1env = get_proc_env(1)
        if "container" in pid1env:
            return True
        if "LIBVIRT_LXC_UUID" in pid1env:
            return True
    except (IOError, OSError):
        pass

    # Detect OpenVZ containers
    if os.path.isdir("/proc/vz") and not os.path.isdir("/proc/bc"):
        return True

    try:
        # Detect Vserver containers
        lines = load_file("/proc/self/status").splitlines()
        for line in lines:
            if line.startswith("VxID:"):
                (_key, val) = line.strip().split(":", 1)
                if val != "0":
                    return True
    except (IOError, OSError):
        pass

    return False


def get_proc_env(pid):
    """
    Return the environment in a dict that a given process id was started with.
    """

    env = {}
    fn = os.path.join("/proc/", str(pid), "environ")
    try:
        contents = load_file(fn)
        toks = contents.split("\0")
        for tok in toks:
            if tok == "":
                continue
            (name, val) = tok.split("=", 1)
            if name:
                env[name] = val
    except (IOError, OSError):
        pass
    return env


def keyval_str_to_dict(kvstring):
    ret = {}
    for tok in kvstring.split():
        try:
            (key, val) = tok.split("=", 1)
        except ValueError:
            key = tok
            val = True
        ret[key] = val
    return ret
