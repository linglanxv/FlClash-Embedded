#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向 FlClash 源码注入"内嵌订阅"逻辑：

1) 首次启动（本机没有任何订阅时）自动导入订阅，之后按 FlClash 默认的
   autoUpdate 周期自动刷新，无需每天手动更新。
2) 每次启动都会校验订阅地址：若 subscription_url.txt 变更后重新编译，
   旧 profile 的 url 会被自动替换为新地址并立即重新拉取。
3) 支持多个订阅源（一行一个，按顺序优先），启动时用 HEAD 探测可用性，
   自动回退到可用源，避免单一 CDN 不可用导致导入失败。
4) --force-refresh 打开后，每次启动都强制重新下载一次订阅内容。

脚本幂等：重复运行不会重复注入。
注入点基于正则锚点匹配，并有降级锚点；全部失败时报错退出（不静默）。
"""
import argparse
import pathlib
import re
import sys

FUNC_TEMPLATE = '''

// ==== [embedded-subscription] auto-generated, DO NOT EDIT ====
const String kEmbeddedSubscriptionLabel = '__LABEL__';
const bool kEmbeddedForceRefreshOnLaunch = __FORCE__;
const List<String> kEmbeddedSubscriptionUrls = <String>[
__URL_LIST__
];

Future<bool> _probeEmbeddedUrl(String url) async {
  try {
    final client = HttpClient()
      ..connectionTimeout = const Duration(seconds: 10);
    // HEAD only: do not download the whole profile just to probe it.
    final request = await client.openUrl('HEAD', Uri.parse(url));
    request.headers.set(HttpHeaders.userAgentHeader, 'clash.meta');
    final response = await request.close();
    final ok = response.statusCode >= 200 && response.statusCode < 300;
    await response.drain<void>();
    client.close(force: true);
    return ok;
  } catch (e) {
    print('[embedded-subscription] probe failed: $url ($e)');
    return false;
  }
}

Future<String?> _pickEmbeddedUrl() async {
  for (final url in kEmbeddedSubscriptionUrls) {
    if (await _probeEmbeddedUrl(url)) {
      print('[embedded-subscription] using source: $url');
      return url;
    }
  }
  if (kEmbeddedSubscriptionUrls.isEmpty) {
    return null;
  }
  print('[embedded-subscription] all sources unreachable, try primary anyway');
  return kEmbeddedSubscriptionUrls.first;
}

Future<void> _initEmbeddedProfile() async {
  for (var i = 0; i < 30; i++) {
    await Future.delayed(const Duration(seconds: 5));
    try {
      final profiles = await database.profilesDao.query().get();
      final action = _container.read(profilesActionProvider.notifier);
      final matched = profiles
          .where((p) => p.label == kEmbeddedSubscriptionLabel)
          .toList();
      if (matched.isEmpty) {
        // User already has other profiles: do not force-insert ours.
        if (profiles.isNotEmpty) {
          return;
        }
        final url = await _pickEmbeddedUrl();
        if (url == null) {
          return;
        }
        final profile = Profile.normal(
          label: kEmbeddedSubscriptionLabel,
          url: url,
        );
        action.putProfile(profile);
        await action.updateProfile(profile);
        print('[embedded-subscription] subscription imported');
        return;
      }
      final profile = matched.first;
      var needUpdate = kEmbeddedForceRefreshOnLaunch;
      if (!needUpdate) {
        // Re-download when the local profile file is missing, or when the
        // baked-in source list no longer contains the url of this profile.
        final path = await appPath.getProfilePath(profile.id.toString());
        needUpdate = !await File(path).exists() ||
            !kEmbeddedSubscriptionUrls.contains(profile.url);
      }
      if (!needUpdate) {
        return;
      }
      final url = await _pickEmbeddedUrl();
      if (url == null) {
        return;
      }
      await action.updateProfile(
        profile.copyWith(url: url, lastUpdateDate: null),
      );
      print('[embedded-subscription] profile refreshed from $url');
      return;
    } catch (e) {
      print('[embedded-subscription] auto import failed (attempt $i): $e');
    }
  }
}
// ==== [/embedded-subscription] ====
'''

CALL_LINE = 'unawaited(_initEmbeddedProfile());'

# 主锚点：bootstrap.dart _initApp() 中的订阅自动更新调用语句
PRIMARY_ANCHOR = re.compile(
    r'unawaited\(\s*_container\.read\(profilesActionProvider\.notifier\)'
    r'\.autoUpdateProfiles\(\)\s*,?\s*\);'
)

# 降级锚点：_initApp 函数体开头
FALLBACK_ANCHOR = re.compile(
    r'(_initApp\(\)\s*(?:async\s*)?\{)'
)

# 注入代码依赖的上游 API/符号
REQUIRED_BOOTSTRAP = ('profilesActionProvider', 'profilesDao', 'appPath')
REQUIRED_ACTIONS = ('void putProfile(', 'updateProfile(')
REQUIRED_MODEL = 'factory Profile.normal('


def read_urls(path: pathlib.Path) -> list:
    urls = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if "'" in line:
            raise SystemExit("ERROR: invalid url (contains quote): %s" % line)
        if not line.startswith('http'):
            raise SystemExit("ERROR: invalid url: %s" % line)
        if line not in urls:
            urls.append(line)
    if not urls:
        raise SystemExit("ERROR: no subscription url found in %s" % path)
    return urls


def check_api(path: pathlib.Path, needles: tuple, label: str) -> bool:
    if not path.exists():
        print('WARNING: %s not found, skip check for %s' % (path, label))
        return True
    src = path.read_text(encoding='utf-8')
    missing = [x for x in needles if x not in src]
    if missing:
        print('ERROR: upstream %s changed, missing in %s: %s'
              % (label, path, ', '.join(missing)), file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description='Inject embedded subscription into FlClash source')
    parser.add_argument('--url-file', required=True, help='订阅 URL 列表文件，一行一个（可多行做备用源）')
    parser.add_argument('--label', default='AutoMergePublicNodes', help='订阅显示名称')
    parser.add_argument('--force-refresh', action='store_true',
                        help='每次启动都强制重新下载订阅内容')
    parser.add_argument('--source', default='lib/bootstrap.dart', help='注入目标文件')
    parser.add_argument('--api-file', default='lib/providers/actions/profiles.dart',
                        help='用于校验上游 API 是否存在的文件')
    parser.add_argument('--model-file', default='lib/models/profile.dart',
                        help='用于校验 Profile 模型 API 的文件')
    args = parser.parse_args()

    urls = read_urls(pathlib.Path(args.url_file))

    target = pathlib.Path(args.source)
    src = target.read_text(encoding='utf-8')

    # 校验依赖的上游 API，避免注入后编译才报错
    if not check_api(pathlib.Path(args.api_file), REQUIRED_ACTIONS, 'ProfilesAction API'):
        return 1
    if not check_api(pathlib.Path(args.model_file), (REQUIRED_MODEL,), 'Profile model'):
        return 1
    missing = [x for x in REQUIRED_BOOTSTRAP if x not in src]
    if missing:
        print('ERROR: upstream code changed, missing in %s: %s' % (target, ', '.join(missing)),
              file=sys.stderr)
        return 1

    if '[embedded-subscription]' in src:
        print('[embedded-subscription] already injected, skip')
        return 0

    url_list = '\n'.join("  '%s'," % u for u in urls)
    func_code = (FUNC_TEMPLATE
                 .replace('__URL_LIST__', url_list)
                 .replace('__LABEL__', args.label)
                 .replace('__FORCE__', 'true' if args.force_refresh else 'false'))

    inserted = False
    m = PRIMARY_ANCHOR.search(src)
    if m:
        line_start = src.rfind('\n', 0, m.start()) + 1
        indent = re.match(r'[ \t]*', src[line_start:]).group(0)
        src = src[:m.end()] + '\n' + indent + CALL_LINE + src[m.end():]
        inserted = True
        print('[embedded-subscription] call inserted after autoUpdateProfiles()')
    else:
        m = FALLBACK_ANCHOR.search(src)
        if m:
            line_start = src.rfind('\n', 0, m.start()) + 1
            indent = re.match(r'[ \t]*', src[line_start:]).group(0) + '  '
            src = src[:m.end()] + '\n' + indent + CALL_LINE + src[m.end():]
            inserted = True
            print('[embedded-subscription] call inserted at _initApp() entry (fallback anchor)')

    if not inserted:
        print('ERROR: no insertion anchor found in %s; please review upstream changes.' % target,
              file=sys.stderr)
        return 1

    src = src.rstrip('\n') + '\n' + func_code
    target.write_text(src, encoding='utf-8')

    # 自检
    final = target.read_text(encoding='utf-8')
    ok = ('kEmbeddedSubscriptionUrls' in final
          and '_initEmbeddedProfile' in final
          and all(u in final for u in urls))
    if not ok:
        print('ERROR: self-check failed after injection', file=sys.stderr)
        return 1
    print('[embedded-subscription] injected OK -> %s' % target)
    print('  label        : %s' % args.label)
    print('  force refresh: %s' % ('true' if args.force_refresh else 'false'))
    for i, u in enumerate(urls, 1):
        print('  source %-5d: %s' % (i, u))
    return 0


if __name__ == '__main__':
    sys.exit(main())
