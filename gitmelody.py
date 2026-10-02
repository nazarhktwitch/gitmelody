#!/usr/bin/env python3
"""Turn a git repository's commit history into a MIDI or WAV file"""

import argparse
import hashlib
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array
from collections import namedtuple
from datetime import datetime

import mido

TICKS_PER_BEAT = 480
SAMPLE_RATE = 44100
MAX_GAP_HOURS = 2 * 365 * 24
MAX_REST_TICKS = TICKS_PER_BEAT * 4
MIN_DURATION_TICKS = TICKS_PER_BEAT // 4
MAX_DURATION_TICKS = TICKS_PER_BEAT * 2
MAX_LINES = 10000.0
BASE_PITCH = 45
SCALE = [0, 3, 5, 7, 10]
DRUM_CHANNEL = 9

Commit = namedtuple('Commit', ['hash', 'author', 'email', 'date', 'subject', 'parents', 'lines'])
Note = namedtuple('Note', ['start', 'duration', 'pitches', 'velocity', 'email'])


def is_url(path):
    if '://' in path:
        return True
    if ':' in path and '@' in path:
        before_colon = path.split(':', 1)[0]
        if '@' in before_colon and '/' not in before_colon and '\\' not in before_colon:
            return True
    return False


def run_git(args):
    return subprocess.run(['git'] + args, capture_output=True)


def clone_bare(url):
    tmp = tempfile.mkdtemp(prefix='gitmelody-')
    proc = run_git(['clone', '--bare', url, tmp])
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        err = proc.stderr.decode('utf-8', 'replace').strip()
        print(' '.join(err.split()), file=sys.stderr)
        sys.exit(1)
    return tmp


def is_git_repo(path):
    return run_git(['-C', path, 'rev-parse', '--git-dir']).returncode == 0


def parse_log(text):
    entries = []
    fields = None
    lines_changed = 0
    for line in text.split('\n'):
        if '\x1f' in line:
            if fields is not None:
                entries.append((fields, lines_changed))
            fields = line.split('\x1f')
            lines_changed = 0
        elif fields is not None and line.strip():
            cols = line.split('\t')
            if len(cols) >= 3:
                added = 0 if cols[0] == '-' else int(cols[0])
                deleted = 0 if cols[1] == '-' else int(cols[1])
                lines_changed += added + deleted
    if fields is not None:
        entries.append((fields, lines_changed))
    commits = []
    for entry_fields, lines in entries:
        if len(entry_fields) != 6:
            continue
        try:
            date = datetime.fromisoformat(entry_fields[3])
        except ValueError:
            continue
        commits.append(Commit(entry_fields[0], entry_fields[1], entry_fields[2], date,
                              entry_fields[5], entry_fields[4], lines))
    return commits


def fetch_commits(repo_path, args):
    fmt = '%H%x1f%an%x1f%ae%x1f%aI%x1f%P%x1f%s'
    cmd = ['-c', 'core.quotepath=false', '-C', repo_path, 'log', '--numstat',
           '--no-decorate', '--format=' + fmt, args.branch]
    if args.since:
        cmd.append('--since=' + args.since)
    if args.until:
        cmd.append('--until=' + args.until)
    proc = run_git(cmd)
    if proc.returncode != 0:
        err = proc.stderr.decode('utf-8', 'replace').strip()
        print(' '.join(err.split()) or 'git log failed', file=sys.stderr)
        sys.exit(1)
    commits = parse_log(proc.stdout.decode('utf-8', 'replace'))
    commits.reverse()
    if args.max_commits and len(commits) > args.max_commits:
        commits = commits[-args.max_commits:]
    return commits


def log_factor(lines):
    return math.log10(1 + lines) / math.log10(1 + MAX_LINES)


def duration_ticks(lines):
    size = MIN_DURATION_TICKS + log_factor(lines) * (MAX_DURATION_TICKS - MIN_DURATION_TICKS)
    return int(round(size))


def velocity(lines):
    return max(1, min(127, int(round(20 + log_factor(lines) * 107))))


def pitch(hour):
    pos = hour * 15 // 24
    return BASE_PITCH + (pos // 5) * 12 + SCALE[pos % 5]


def rest_ticks(gap_seconds):
    hours = gap_seconds / 3600.0
    factor = math.log10(1 + hours) / math.log10(1 + MAX_GAP_HOURS)
    return int(round(factor * MAX_REST_TICKS))


def commits_to_notes(commits):
    notes = []
    prev_date = None
    for commit in commits:
        gap = (commit.date - prev_date).total_seconds() if prev_date else 0
        prev_date = commit.date
        rest = rest_ticks(gap)
        vel = velocity(commit.lines)
        if len(commit.parents.split()) >= 2:
            root = pitch(commit.date.hour)
            notes.append(Note(rest, duration_ticks(commit.lines),
                              [root, root + 3, root + 7], vel, commit.email))
        elif commit.subject.startswith('Revert'):
            root = pitch(commit.date.hour)
            short = TICKS_PER_BEAT // 2
            notes.append(Note(rest, short, [root], vel, commit.email))
            notes.append(Note(0, short, [root - 4], vel, commit.email))
        else:
            notes.append(Note(rest, duration_ticks(commit.lines),
                              [pitch(commit.date.hour)], vel, commit.email))
    tick = 0
    timed = []
    for note in notes:
        tick += note.start
        timed.append(Note(tick, note.duration, note.pitches, note.velocity, note.email))
        tick += note.duration
    return timed


def channel_for(email, channels):
    if email not in channels:
        available = [c for c in range(16) if c != DRUM_CHANNEL]
        channels[email] = available[len(channels) % len(available)]
    return channels[email]


def instrument_for(email, used):
    # md5 is stable across runs, unlike hash()
    digest = hashlib.md5(email.encode('utf-8', 'replace')).digest()
    program = int.from_bytes(digest[:4], 'big') % 128
    while program in used:
        program = (program + 1) % 128
    used.add(program)
    return program


def write_midi(path, notes, tempo_bpm, instrument_map):
    mid = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(tempo_bpm), time=0))
    channels = {}
    used_programs = set()
    programmed = set()
    last_tick = 0
    for note in notes:
        channel = channel_for(note.email, channels)
        delta = note.start - last_tick
        if instrument_map and note.email not in programmed:
            program = instrument_for(note.email, used_programs)
            track.append(mido.Message('program_change', channel=channel,
                                      program=program, time=delta))
            programmed.add(note.email)
            delta = 0
        for i, p in enumerate(note.pitches):
            track.append(mido.Message('note_on', channel=channel, note=p,
                                      velocity=note.velocity, time=delta if i == 0 else 0))
        for i, p in enumerate(note.pitches):
            track.append(mido.Message('note_off', channel=channel, note=p,
                                      velocity=64, time=note.duration if i == 0 else 0))
        last_tick = note.start + note.duration
    mid.save(path)


def mix_note(buf, start, duration_secs, pitches, velocity):
    count = int(duration_secs * SAMPLE_RATE)
    if count <= 0:
        return
    amp = velocity / 127.0 * 0.2
    attack = min(int(0.005 * SAMPLE_RATE), count // 2)
    release = min(int(0.05 * SAMPLE_RATE), count // 2)
    freqs = [440.0 * 2 ** ((p - 69) / 12.0) for p in pitches]
    for i in range(count):
        t = i / SAMPLE_RATE
        s = 0.0
        for j, f in enumerate(freqs):
            s += math.sin(2 * math.pi * f * t) / (j + 1)
        if attack and i < attack:
            s *= i / attack
        if release and i >= count - release:
            s *= (count - i) / release
        buf[start + i] += s * amp


def write_wav(path, notes, tempo_bpm):
    seconds_per_tick = 60.0 / (tempo_bpm * TICKS_PER_BEAT)
    total_ticks = notes[-1].start + notes[-1].duration
    buf = array('f', [0.0]) * int(total_ticks * seconds_per_tick * SAMPLE_RATE + 1)
    for note in notes:
        start = int(note.start * seconds_per_tick * SAMPLE_RATE)
        mix_note(buf, start, note.duration * seconds_per_tick, note.pitches, note.velocity)
    frames = array('h')
    for s in buf:
        frames.append(int(max(-1.0, min(1.0, s)) * 32767))
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(frames.tobytes())


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description='Turn a git repository\'s commit history into a MIDI or WAV file')
    parser.add_argument('repo', help='path to a git repository or a git URL')
    parser.add_argument('-o', '--output', required=True, help='output file (.mid or .wav)')
    parser.add_argument('--since', help='only commits after this date')
    parser.add_argument('--until', help='only commits before this date')
    parser.add_argument('--branch', default='HEAD', help='branch to read (default: current HEAD)')
    parser.add_argument('--max-commits', type=int, help='keep only the most recent N commits')
    parser.add_argument('--tempo', type=float, default=120, help='tempo in BPM (default: 120)')
    parser.add_argument('--instrument-map', action='store_true',
                        help='give each author a different instrument')
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    if shutil.which('git') is None:
        print('git is not installed or not in PATH', file=sys.stderr)
        sys.exit(1)
    tmp_dir = None
    if is_url(args.repo):
        repo_path = clone_bare(args.repo)
        tmp_dir = repo_path
    else:
        repo_path = args.repo
        if not is_git_repo(repo_path):
            print('not a git repository: ' + repo_path, file=sys.stderr)
            sys.exit(1)
    try:
        commits = fetch_commits(repo_path, args)
        if not commits:
            print('no commits found', file=sys.stderr)
            sys.exit(1)
        notes = commits_to_notes(commits)
        ext = os.path.splitext(args.output)[1].lower()
        if ext == '.mid':
            write_midi(args.output, notes, args.tempo, args.instrument_map)
        elif ext == '.wav':
            write_wav(args.output, notes, args.tempo)
        else:
            print('unknown output format: ' + ext + ' (use .mid or .wav)', file=sys.stderr)
            sys.exit(1)
        print('wrote {} notes to {}'.format(len(notes), args.output))
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == '__main__':
    main(sys.argv[1:])
