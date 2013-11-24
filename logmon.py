import os
import os.path
import re
import ast
import sys
import time
import logging
import datetime as dt
from operator import itemgetter as ith
from itertools import groupby as groupby
from functools import partial
import smtplib
from email.mime.text import MIMEText

def fsize(fname):
    _1MB = 1024. * 1024
    return os.path.getsize(fname) / _1MB

def min_since_last_update(fname):
    epoch_last = os.path.getmtime(fname)
    epoch_now = time.mktime(dt.datetime.now().timetuple())
    return (epoch_now - epoch_last) / 60

def split_blocks(data, mark):
    lines = data.splitlines(True)
    if len(lines) <= 1:
        return lines
    starts = [1 if re.match(mark, l) else 0 for l in lines]
    keys = reduce(lambda a, x: a + [a[-1] + x], starts, [0])[1:]
    return [''.join(map(ith(1), g)) for k, g in groupby(zip(keys, lines), ith(0))]

def blocks_len(blocks):
    return sum(map(len, blocks))

def adjust_by_state(blocks):
    if has_state():
        return blocks

    lookback = cfg['init_scan_lookback']
    if lookback <= 0:
        return []
    else:
        return blocks[-lookback:]

def has_state():
    return os.path.exists(state_fn)

def load_state():
    init = {target_file: 0}
    if not has_state():
        return init
    with open(state_fn) as f:
        data = f.read()
    try:
        _state = ast.literal_eval(data)
        assert target_file in _state
        return _state
    except:
        logging.exception('load_state failed, data = "%s"', data)
        return init

def save_state(s):
    with open(state_fn, 'w') as f:
        f.write('%s\n' % repr(s))

def load_logs(log_fn, pos):
    with open(log_fn) as f:
        f.seek(pos)
        return f.read()

def check_match(block, **kw):
    try:
        for r in cfg['match_rules']:
            if re.search(r['regex'], block, re.M):
                r['action'](**kw)
    except:
        logging.exception('Failed on block: %s', block)

def extract_body(blocks, i, before, after):
    if i < before:
        s = load_state()
        lookback = 4 * 1024 # internal cfg
        data = load_logs(target_file, max(0, s[target_file] - lookback))
        more_blocks = split_blocks(data, cfg['block_mark'])[:-1]
        i += len(more_blocks) - len(blocks)
        blocks = more_blocks

    start = max(0, i - before)
    end = i + after + 1
    ctx_before = ''.join(blocks[start:i])
    ctx_after = ''.join(blocks[i+1:end])
    sep = cfg['email_context_separator']
    return sep.join([ctx_before, blocks[i], ctx_after])

def do_send_email(subject, body):
    host, port = cfg['smtp_host'], cfg['smtp_port']
    sender, pwd = cfg['smtp_user'], cfg['smtp_pwd']
    recipients = cfg['email_send_to']

    msg = MIMEText(body, _charset='utf-8')
    msg['To'] = ';'.join(recipients)
    msg['From'] = sender
    msg['Subject'] = subject

    session = smtplib.SMTP(host, port, timeout=10) # internal cfg
    try:
        session.ehlo()
        if session.has_extn('STARTTLS'):
            session.starttls()
            session.ehlo()
        session.login(sender, pwd)
        session.sendmail(sender, recipients, msg.as_string())
    finally:
        try:
            session.quit()
        except:
            logging.exception('Failed to terminate SMTP session') # log only, no raise

def send_email(subj_pars, body_pars, **kw):
    if subj_pars['extract']:
        blocks, i = kw['blocks'], kw['i']
        subject = re.search(subj_pars['pattern'], blocks[i].split('\n', 1)[0]).group()
    else:
        subject = subj_pars['subject']
    if body_pars['extract']:
        blocks, i = kw['blocks'], kw['i']
        body = extract_body(blocks, i, body_pars['before'], body_pars['after'])
    else:
        body = body_pars['body']

    for retry_cd in [5, 10, 0]:
        try:
            do_send_email(subject, body)
        except:
            if retry_cd > 0:
                logging.exception('Failed to send email, will retry in %s second(s)', retry_cd)
                time.sleep(retry_cd)
            else:
                logging.exception('Failed to send email')
                break
        else:
            break

def play_sound(path, n, **kw):
    try:
        import winsound
    except ImportError:
        logging.warning('Failed to import winsound')
        return

    for i in range(n):
        winsound.PlaySound(path, winsound.SND_FILENAME)

def parse_int(x):
    if isinstance(x, int):
        return x
    elif isinstance(x, str):
        return int(re.search(r'\d+', x).group())
    else:
        assert False, x

def parse_action(r):
    try:
        assert isinstance(r, dict)
        if 'send_email' in r:
            pars = r['send_email']
            subject, body = pars['subject'], pars.get('body', '')
            if isinstance(subject, tuple): # eg: ("regex", "TWS.*")
                subj_pars = {'extract': True, 'pattern': subject[1]}
            else:
                assert isinstance(subject, str)
                subj_pars = {'extract': False, 'subject': subject}
            if isinstance(body, tuple): # eg: ("extract", 5, 5)
                pmin, pmax = partial(min, 20), partial(max, 0) # internal cfg
                before, after =  map(pmax, map(pmin, map(parse_int, body[1:])))
                body_pars = {'extract': True, 'before': before, 'after': after}
            else:
                assert isinstance(body, str)
                body_pars = {'extract': False, 'body': body}
            if not 'regex' in r: # file rules
                msg = 'Only match rules may extract subject or body from context'
                assert not (subj_pars['extract'] or body_pars['extract']), msg
            return partial(send_email, subj_pars=subj_pars, body_pars=body_pars)
        elif 'play_sound' in r:
            sound_file, repeat = r['play_sound']
            return partial(play_sound, sound_file, parse_int(repeat))
        else:
            assert False
    except:
        logging.exception('Parse action failed: %s', r)
        return lambda *a, **kw: None

def parse_rule(r):
    try:
        if isinstance(r, str):
            r = ast.literal_eval(r)
        assert isinstance(r, dict)
        for k in ['regex', 'file_idle_minutes', 'file_size_max']:
            if k in r:
                v = r[k] if k == 'regex' else parse_int(r[k])
                return {k: v, 'action': parse_action(r)}
        assert False
    except:
        logging.exception('Parse rule failed: %s', r)
        return {}

def parse_moncfg(fname):
    try:
        with open(fname) as f:
            lines = [l.strip() for l in f]
        _cfg = {'block_mark': '', 'email_context_separator': '', 'init_scan_lookback': 100}

        # parse variables
        nvp = {'block_mark': lambda v: v.split('"')[1], 
               'smtp_port': parse_int,
               'init_scan_lookback': parse_int,
               'email_context_separator': lambda v: v + '\n' if v else v,
               'email_send_to': lambda v: map(str.strip, v.split(',')), }
        nvlines = filter(lambda l: re.match(r'^\w* *:', l), lines)
        for nv in nvlines:
            name, val = map(str.strip, nv.split(':', 1))
            if name in nvp:
                _cfg[name] = nvp[name](val)
            else:
                _cfg[name] = val

        # parse rules
        rlines = filter(lambda l: re.match(r'^{.*}$', l), lines)
        rules = [parse_rule(r) for r in rlines]
        _cfg['match_rules'] = filter(lambda r: 'regex' in r, rules)
        _cfg['file_rules'] = filter(lambda r: 'file_idle_minutes' in r or 'file_size_max' in r, rules)

        # sanity check
        for k in 'target_file block_mark smtp_host smtp_port smtp_user smtp_pwd email_send_to'.split():
            assert k in _cfg, 'Missing: %s' % k

        return _cfg
    except:
        logging.exception('Failed to parse cfg file')
        raise
    
def moncfg_fname():
    assert len(sys.argv) > 1, 'Usage: python logmon.py <cfg_file>'
    return sys.argv[1]

def log_fname():
    if len(sys.argv) > 2:
        return sys.argv[2]
    else:
        return 'logmon.log'

def check_logfile():
    for r in cfg['file_rules']:
        too_idle = 'file_idle_minutes' in r and min_since_last_update(target_file) > r['file_idle_minutes']
        too_big = 'file_size_max' in r and fsize(target_file) > r['file_size_max']
        if too_idle or too_big:
            r['action']()

def main():
    check_logfile()
    s = load_state()
    data = load_logs(target_file, s[target_file])
    blocks = split_blocks(data, cfg['block_mark'])[:-1] # drop the last block, which may be incomplete
    adj_blocks = adjust_by_state(blocks)
    for i, b in enumerate(adj_blocks):
        check_match(b, blocks=adj_blocks, i=i)
    s[target_file] += blocks_len(blocks)
    save_state(s)

def state_fname(target_file):
    return reduce(lambda acc, x: acc.replace(x, '_'), [':', '\\', '/'], target_file) + '.state'

if __name__ == '__main__':
    logging.basicConfig(filename=log_fname(), format='%(levelname)s:%(asctime)s:%(message)s') # internal cfg
    cfg = parse_moncfg(moncfg_fname())
    target_file = cfg['target_file']
    state_fn = state_fname(target_file)
    main()

