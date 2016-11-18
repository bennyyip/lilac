#!/usr/bin/python3 -u

import os
import sys
import traceback
import glob
import logging
import configparser
import time
from collections import defaultdict

topdir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(topdir, 'libs'))

# pylint: disable=wrong-import-position
from myutils import at_dir, execution_timeout
from toposort import toposort_flatten # pylint: disable=import-error
from nicelogger import enable_pretty_logging
from serializer import PickledData

from . import lib as lilaclib
from .buildsession import BuildSession
from .lib import *

config = configparser.ConfigParser()
config.optionxform = lambda option: option
config.read(topdir+'/../config.ini')

# Setting up enviroment variables
os.environ.update(config.items('enviroment variables'))
os.environ['PATH'] = os.path.join(topdir, '../bin') + ':' + os.environ['PATH']

REPODIR = os.path.expanduser(config.get('repository', 'repodir'))
MYNAME = config.get('lilac', 'name')
MYMASTER = config.get('lilac', 'master')

mydir = os.path.expanduser('~/.lilac')
nvchecker_file = os.path.join(mydir, 'nvchecker.ini')
oldver_file = os.path.join(mydir, 'oldver')
newver_file = os.path.join(mydir, 'newver')
building_packages = set()
nvdata = {}
nv_unchanged = {}
DEPENDS = {}

logger = logging.getLogger(__name__)
build_logger = logging.getLogger('build')

Session = BuildSession(os.path.join(topdir, '../config.ini'))
lilaclib.sendmail = Session.sendmail

def setup_build_logger():
  handler = logging.FileHandler(os.path.join(mydir, 'build.log'))
  handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
  build_logger.addHandler(handler)

def _check_dir(d):
  if isinstance(d, tuple):
    d = d[0]
  return os.path.isdir(os.path.join(REPODIR, d))

def check_depends(name, depends):
  failed = [d for d in depends if not _check_dir(d)]
  if failed:
    logger.error('%s has non-existent depends: %r', name, failed)
    raise FileNotFoundError('%s 的以下依赖不存在：%r' % (name, failed))

def read_dependencies(packages):
  # package should be built before which
  package_and_deps_to_build = {}
  # package should be built with which installed
  dep_to_install = defaultdict(list)
  failed = set()
  examined = set()
  while packages:
    package = packages.pop()
    examined.add(package)
    path = os.path.join(REPODIR, package)
    with at_dir(path):
      try:
        with load_lilac() as mod:
          depends = getattr(mod, 'depends', ())
          if depends:
            check_depends(package, depends)
            ds = [Dependency.get(REPODIR, x) for x in depends]
            dep_to_install[package][:0] = ds
            not_built = {x.pkgbase for x in ds if not x.resolve()}
            packages.update(not_built - examined)
          else:
            not_built = set()
          package_and_deps_to_build[package] = not_built
      except Exception as e:
        tb = traceback.format_exc()
        logger.exception('error while loading lilac.py for %s', package)
        send_error_report(package, exc=(e, tb),
                          subject='为软件包 %s 载入 lilac.py 时失败')
        build_logger.error('%s failed', package)
        failed.add(package)
  return package_and_deps_to_build, dep_to_install, failed

def build_package(package):
  logger.info('building %s', package)
  try:
    n = nvdata.get(package, (nv_unchanged[package],) * 2)
    maintainer = Session.find_maintainer(name=package)
    name, email = maintainer.split('<', 1)
    name = name.strip('" ')
    email = email.rstrip('>')
    Session.set_packager(name, email)
    # TODO: remove after run_cmd done
    os.environ['PACKAGER'] = '%s (on behalf of %s) <%s>' % (MYNAME, name, email)
    if is_nodejs_thing():
      # nodejs things have bad error handling. If they go mad, allow them to
      # be mad only one hour.
      time_limit = 1
    else:
      # wait at most 6 hours
      time_limit = 6
    try:
      with execution_timeout(time_limit * 3600):
        lilac_build(
          REPODIR,
          oldver = n[0], newver = n[1],
          depends = DEPENDS.get(package, ()),
        )
    except TimeoutError:
      kill_child_processes()
      raise
    Session.sign_and_copy(package)
    lilaclib.build_output = None
    build_logger.info('%s successful', package)
    return True
  except (MissingDependencies, BuildPrefixError):
    build_logger.error('%s failed', package)
    raise
  except Exception as e:
    tb = traceback.format_exc()
    logger.exception('packaging error')
    send_error_report(package, exc=(e, tb))
    build_logger.error('%s failed', package)

def send_error_report(name, *, msg=None, exc=None, subject=None):
  if msg is None and exc is None:
    raise TypeError('send_error_report received inefficient args')

  who, tb_find = Session.find_maintainer_or_admin(name)

  msgs = []
  if msg is not None:
    msgs.append(msg)

  if exc is not None:
    exception, tb = exc
    if isinstance(exception, CalledProcessError):
      subject = subject or '在编译软件包 %s 时发生错误'
      msgs.append('命令执行失败！\n\n命令 %r 返回了错误号 %d。' \
                  '命令的输出如下：\n\n%s' % (
                    exception.cmd, exception.returncode, exception.output))
      msgs.append('调用栈如下：\n\n' + tb)
    elif isinstance(exception, AurDownloadError):
      subject = subject or '在获取AUR包 %s 时发生错误'
      msgs.append('获取AUR包失败！\n\n')
      msgs.append('调用栈如下：\n\n' + tb)
    else:
      subject = subject or '在编译软件包 %s 时发生未知错误'
      msgs.append('发生未知错误！调用栈如下：\n\n' + tb)

  if '%s' in subject:
    subject = subject % name

  if tb_find:
    msgs.append('获取维护者信息也失败了！调用栈如下：\n\n' + tb_find)
  if lilaclib.build_output:
    msgs.append('编译命令输出如下：\n\n' + lilaclib.build_output)

  msg = '\n'.join(msgs)
  logger.debug('mail to %s:\nsubject: %s\nbody: %s', who, subject, msg[:200])
  Session.sendmail(who, subject, msg)

def packages_need_update(U):
  full = configparser.ConfigParser(dict_type=dict, allow_no_value=True)
  nvchecker_full = os.path.expanduser(os.path.join(REPODIR, 'nvchecker.ini'))
  try:
    full.read([nvchecker_full])
  except:
    tb = traceback.format_exc()
    who, more = Session.find_maintainer_or_admin(file='nvchecker.ini')

    subject = 'nvchecker 配置文件错误'
    msg = '调用栈如下：\n\n' + tb
    if more:
      msg += '\n获取维护者信息也失败了！调用栈如下：\n\n' + more
    Session.sendmail(who, subject, msg)
    raise

  all_known = set(full.sections())
  unknown = U - all_known
  if unknown:
    logger.warning('unknown packages: %r', unknown)

  newconfig = {k: full[k] for k in U & all_known}
  newconfig['__config__'] = {
    'oldver': oldver_file,
    'newver': newver_file,
  }
  new = configparser.ConfigParser(dict_type=dict, allow_no_value=True)
  new.read_dict(newconfig)
  with open(nvchecker_file, 'w') as f:
    new.write(f)
  output = run_cmd(['nvchecker', nvchecker_file])

  error = False
  errorlines = []
  for l in output.splitlines():
    if l.startswith('[E'):
      error = True
    elif l.startswith('['):
      error = False
    if error:
      errorlines.append(l)

  if unknown or errorlines:
    subject = 'nvchecker 问题'
    msg = ''
    if unknown:
      msg += '以下软件包没有相应的更新配置信息：\n\n' + ''.join(
        x + '\n' for x in sorted(unknown)) + '\n'
    if errorlines:
      msg += '以下软件包在更新检查时出错了：\n\n' + '\n'.join(
        errorlines) + '\n'
    Session.send_repo_mail(subject, msg)

  for x in run_cmd(['nvcmp', nvchecker_file]).splitlines():
    pkg, oldver, _, newver = x.split()
    if oldver == 'None':
      oldver = None
    nvdata[pkg] = oldver, newver
  with open(newver_file) as f:
    nv_unchanged.update(x.split(None, 1) for x in f)

  updated = set(nvdata.keys())
  return updated, unknown

def all_packages_I_manage():
  r = glob.glob('*/lilac.py')
  return {x.split('/', 1)[0] for x in r}

def start_build(*, dont_build_deps=False):
  global DEPENDS
  if dont_build_deps:
    packages = building_packages
    failed = set()
  else:
    package_and_deps_to_build, dep_to_install, failed = read_dependencies(building_packages)
    packages = toposort_flatten(package_and_deps_to_build)

    # used to decide what to install when building
    DEPENDS = dep_to_install

  built = set()
  try:
    logger.info('building these packages: %r', packages)
    for pkg in packages:
      if pkg in failed:
        # marked as failed, skip
        continue

      path = os.path.join(REPODIR, pkg)
      with at_dir(path):
        try:
          if build_package(pkg):
            built.add(pkg)
          else:
            failed.add(pkg)

        except BuildPrefixError as e:
          send_error_report(pkg, subject='lilac.py 脚本不被支持',
                            msg = '''\
软件包 {pkg} 的 build_prefix {bp!r} 不再被支持。
注意：32位软件包的支持已经被放弃。如有需要，请自行编译后上传。'''.format(
            pkg = pkg, bp = e.build_prefix))
          failed.add(pkg)

        except MissingDependencies as e:
          reason = ''

          faileddeps = e.deps & failed
          if faileddeps:
            reason += '唔，这些包没能成功打包呢：%r' % faileddeps

          send_error_report(pkg, subject='%s 出现依赖问题',
                            msg = '''\
  在成功地编译打包 {built} 之后，{pkg} 依旧依赖 {deps}。

  {reason}'''.format(
    built = built, deps = e.deps, pkg = pkg, reason = reason,
  ))
          failed.add(pkg)

  except KeyboardInterrupt:
    logger.info('keyboard interrupted, bye~')

  return failed

def main(packages=None):
  store = os.path.join(mydir, 'store')
  with PickledData(store, default={}) as D:
    try:
      failed_info = D.get('failed', {})

      if packages is None:
        git_reset_hard()
        git_pull()

        U = all_packages_I_manage()
        last_commit = D.get('last_commit', EMPTY_COMMIT)
        revisions = last_commit + '..HEAD'
        changed = get_changed_packages(revisions, U)
        updated, unknown = packages_need_update(U)

        failed_prev = set(failed_info.keys())
        failed_updated = {k for k, v in failed_info.items()
                          if k in nvdata and nvdata[k][1] != v}
        # build updated; if last build failed but it gets updated once more,
        # build it again
        need_update = updated | failed_updated
        # no update from upstream, but build instructions have changed; rebuild
        # failed ones
        need_rebuild_failed = failed_prev & changed
        # if pkgrel is updated, build a new release
        need_rebuild_pkgrel = {x for x in changed
                               if pkgrel_changed(revisions, x)} - unknown
        all_building = need_update | need_rebuild_failed | need_rebuild_pkgrel

        logger.info('these updated (pkgrel) packages should be rebuilt: %r',
                    need_rebuild_pkgrel or None)
        logger.info('these previously-failed packages should be rebuilt: %r',
                    need_rebuild_failed or None)
        logger.info('these packages are updated as detected by nvchecker: %r',
                    need_update or None)
      else:
        all_building = set(packages)
        updated, unknown = packages_need_update(all_building)
        need_update = updated
        logger.info('these packages are manually specified: %r', all_building)

      dont_build_deps = bool(packages)

      building_packages.update(all_building)
      failed = start_build(dont_build_deps=dont_build_deps)

      if packages is None:
        D['last_commit'] = git_last_commit()
      update_succeeded = all_building - failed
      failed_info.update({k: nvdata[k][1] for k in failed if k in nvdata})
      for x in update_succeeded:
        if x in failed_info:
          del failed_info[x]
      D['failed'] = failed_info

      if config.getboolean('lilac', 'rebuild_failed_pkgs'):
        if update_succeeded:
          run_cmd(['nvtake', nvchecker_file] + list(update_succeeded))
      else:
        if need_update:
          run_cmd(['nvtake', nvchecker_file] + list(need_update))

      if packages is None:
        git_reset_hard()
        if config.getboolean('lilac', 'git_push'):
          git_push()
    except Exception:
      tb = traceback.format_exc()
      logger.exception('unexpected error')
      subject = '运行时错误'
      msg = '调用栈如下：\n\n' + tb
      Session.send_master_mail(subject, msg)

def setup():
  if config.getboolean('lilac', 'log_to_file'):
    os.makedirs(os.path.join(mydir, 'log'), exist_ok=True)
    logfile = os.path.join(mydir, 'log', time.strftime('%Y-%m-%dT%H:%M:%S'))
    fd = os.open(logfile, os.O_WRONLY | os.O_CREAT, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)

  enable_pretty_logging('DEBUG')

  # TODO: remove after all run_cmds have been replaced
  if 'MAKEFLAGS' not in os.environ:
    cores = os.cpu_count()
    if cores is not None:
      os.environ['MAKEFLAGS'] = '-j{0} -l{0}'.format(cores)

  if not os.path.exists(oldver_file):
    open(oldver_file, 'a').close()

  aur_repo_dir = os.path.join(mydir, 'aur')
  os.makedirs(aur_repo_dir, exist_ok=True)
  lilaclib.AUR_REPO_DIR = aur_repo_dir

  setup_build_logger()
  os.chdir(REPODIR)
  lilaclib.send_error_report = send_error_report

if __name__ == '__main__':
  try:
    setup()

    if len(sys.argv) == 1:
      main()
    else:
      main(sys.argv[1:])
  except Exception:
    logger.exception('unexpected error')
