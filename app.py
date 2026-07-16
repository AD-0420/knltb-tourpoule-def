import csv
import io
import json
import os
import unicodedata
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, flash, abort, session
from functools import wraps
from rapidfuzz import fuzz, process as fuzz_process
from models import (db, Setting, Cluster, Participant, Rider, RoodEntry, Selection, Stage,
                    StageResult, JerseyWearer, FinalClassification,
                    BonusQuestion, BonusAnswer)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'knltb-tourpoule-2026-dev-only')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///tourpoule.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['ADMIN_PASSWORD'] = os.environ.get('ADMIN_PASSWORD', 'tourpoule2026')

db.init_app(app)

# Maak databasetabellen automatisch aan bij opstarten (ook op Railway/productie)
with app.app_context():
    db.create_all()
    # Migrate: add columns to existing rider tables
    with db.engine.connect() as _conn:
        for ddl in [
            "ALTER TABLE rider ADD COLUMN niet_gestart BOOLEAN DEFAULT 0",
            "ALTER TABLE rider ADD COLUMN team VARCHAR(150)",
            "ALTER TABLE participant ADD COLUMN edit_token VARCHAR(36)",
        ]:
            try:
                _conn.execute(db.text(ddl))
                _conn.commit()
            except Exception:
                pass
        # Generate edit tokens for any participants that don't have one yet
        rows = _conn.execute(db.text(
            "SELECT id FROM participant WHERE edit_token IS NULL"
        )).fetchall()
        for row in rows:
            _conn.execute(db.text(
                "UPDATE participant SET edit_token = :tok WHERE id = :id"
            ), {"tok": str(uuid.uuid4()), "id": row[0]})
        if rows:
            _conn.commit()

POINTS_TABLE = {1: 35, 2: 25, 3: 20, 4: 18, 5: 16, 6: 14, 7: 12,
                8: 10, 9: 8, 10: 6, 11: 5, 12: 4, 13: 3, 14: 2, 15: 1}
BONUS_POINTS = 15
JERSEY_DAILY_POINTS = 1
JERSEY_LABELS = {'yellow': 'Gele trui', 'green': 'Groene trui',
                 'polka': 'Bolletjestrui', 'white': 'Witte trui'}
JERSEY_CLASSES = {'yellow': 'jersey-yellow', 'green': 'jersey-green',
                  'polka': 'jersey-polka', 'white': 'jersey-white'}
MAX_GEEL = 20
MAX_ROOD = 15
# Totaal aantal etappes in de Tour de France; bepaalt of de stand een tussenstand
# of de definitieve eindstand is.
TOTAL_STAGES = 21
# Nederlandse tijdzone: de server (Railway) draait in UTC, dus de deadline en alle
# "nu"-vergelijkingen moeten expliciet in Europe/Amsterdam om niet 2 uur af te wijken.
NL_TZ = ZoneInfo('Europe/Amsterdam')
INSCHRIJF_DEADLINE = datetime(2026, 7, 4, 17, 0, 0, tzinfo=NL_TZ)


def now_nl():
    """Huidige tijd in de Nederlandse tijdzone (los van de server-tijdzone)."""
    return datetime.now(NL_TZ)
INSCHRIJFGELD = '€5,-'


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login', next=request.url))
        return f(*args, **kwargs)
    return decorated


def get_setting(key, default=None):
    try:
        s = Setting.query.get(key)
        return s.value if s and s.value is not None else default
    except Exception:
        return default


def set_setting(key, value):
    s = Setting.query.get(key)
    if s:
        s.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()


def eindstand_status():
    """Bepaal de zichtbaarheid van de eindstand.
    - is_final: alle etappes gereden
    - published: admin heeft de eindstand vrijgegeven voor deelnemers
    - visible: mag de huidige gebruiker de eindstand zien
    - admin_preview: admin ziet de eindstand terwijl deze nog niet is vrijgegeven
    """
    try:
        is_final = Stage.query.count() >= TOTAL_STAGES
    except Exception:
        is_final = False
    published = get_setting('eindstand_published', '0') == '1'
    is_admin = bool(session.get('admin_logged_in'))
    visible = is_final and (published or is_admin)
    return {
        'is_final': is_final,
        'published': published,
        'is_admin': is_admin,
        'visible': visible,
        'admin_preview': is_final and is_admin and not published,
    }


def bonus_active():
    """Bonusvragen en -punten tellen pas mee ná de laatste etappe.
    Voor deelnemers worden ze pas zichtbaar zodra de eindstand is vrijgegeven;
    admins zien ze direct na de laatste etappe (preview). Zelfde regel als de
    eindstand-zichtbaarheid."""
    return eindstand_status()['visible']


@app.context_processor
def inject_tour_status():
    """Maak overal (o.a. in de navigatie) beschikbaar of de Tour is afgelopen,
    of de eindstand voor de huidige gebruiker zichtbaar is, en of inschrijven
    nog open is (Inschrijven-knop verbergen na de deadline)."""
    status = eindstand_status()
    return {
        'tour_finished': status['is_final'],
        'eindstand_visible': status['visible'],
        'inschrijving_open': now_nl() <= INSCHRIJF_DEADLINE,
    }


def get_rider_points_map():
    """Return dict of rider_id -> total_points for all riders."""
    from collections import defaultdict
    points = defaultdict(int)

    for result in StageResult.query.all():
        points[result.rider_id] += POINTS_TABLE.get(result.position, 0)

    for jw in JerseyWearer.query.all():
        points[jw.rider_id] += JERSEY_DAILY_POINTS

    for fc in FinalClassification.query.all():
        points[fc.rider_id] += POINTS_TABLE.get(fc.position, 0)

    return points


def get_participant_stage_points(participant_id):
    """Return per-stage cumulative geel points for one participant.
    Returns list of {'stage': int, 'pts': int, 'cumul': int} sorted by stage.
    """
    from collections import defaultdict
    p = Participant.query.get(participant_id)
    if not p:
        return []

    geel_rider_ids = {s.rider_id for s in p.selections if s.type == 'geel'}
    stage_pts = defaultdict(int)

    for result in StageResult.query.all():
        if result.rider_id in geel_rider_ids:
            stage_pts[result.stage_id] += POINTS_TABLE.get(result.position, 0)

    for jw in JerseyWearer.query.all():
        if jw.rider_id in geel_rider_ids:
            stage_pts[jw.stage_id] += JERSEY_DAILY_POINTS

    stages = Stage.query.order_by(Stage.number).all()
    stage_id_to_num = {s.id: s.number for s in stages}

    rows = []
    cumul = 0
    for stage in stages:
        pts = stage_pts.get(stage.id, 0)
        cumul += pts
        rows.append({'stage': stage.number, 'pts': pts, 'cumul': cumul})

    return rows


def get_participant_stage_breakdown(participant_id):
    """Per etappe: welke geel-renners van deze deelnemer punten scoorden en hoeveel.
    Returnt lijst van {'stage': num, 'total': int, 'riders': [{'name','points','jerseys'}]}
    voor alle ingevoerde etappes, gesorteerd op etappenummer."""
    from collections import defaultdict
    p = Participant.query.get(participant_id)
    if not p:
        return []
    geel_ids = {s.rider_id for s in p.selections if s.type == 'geel'}
    if not geel_ids:
        return []
    names = {r.id: r.name for r in Rider.query.filter(Rider.id.in_(geel_ids)).all()}

    # stage_id -> rider_id -> {'points': int, 'jerseys': [labels]}
    per = defaultdict(lambda: defaultdict(lambda: {'points': 0, 'jerseys': []}))
    for res in StageResult.query.all():
        if res.rider_id in geel_ids:
            per[res.stage_id][res.rider_id]['points'] += POINTS_TABLE.get(res.position, 0)
    for jw in JerseyWearer.query.all():
        if jw.rider_id in geel_ids:
            per[jw.stage_id][jw.rider_id]['points'] += JERSEY_DAILY_POINTS
            per[jw.stage_id][jw.rider_id]['jerseys'].append(
                JERSEY_LABELS.get(jw.jersey_type, jw.jersey_type))

    breakdown = []
    for stage in Stage.query.order_by(Stage.number).all():
        riders = []
        for rid, info in per.get(stage.id, {}).items():
            if info['points'] > 0:
                riders.append({'name': names.get(rid, '?'),
                               'points': info['points'],
                               'jerseys': info['jerseys']})
        riders.sort(key=lambda x: x['points'], reverse=True)
        breakdown.append({'stage': stage.number,
                          'total': sum(r['points'] for r in riders),
                          'riders': riders})
    return breakdown


def get_stage_day_leaders():
    """Bepaal per etappe welke deelnemer(s) de hoogste dagscore (geel) haalden.
    Returnt dict: stage_id -> {'points': int, 'names': [namen]} (alleen als > 0)."""
    from collections import defaultdict
    stage_rider_pts = defaultdict(lambda: defaultdict(int))
    for r in StageResult.query.all():
        stage_rider_pts[r.stage_id][r.rider_id] += POINTS_TABLE.get(r.position, 0)
    for jw in JerseyWearer.query.all():
        stage_rider_pts[jw.stage_id][jw.rider_id] += JERSEY_DAILY_POINTS

    part_geel = defaultdict(set)
    for s in Selection.query.filter_by(type='geel').all():
        part_geel[s.participant_id].add(s.rider_id)
    part_name = {p.id: p.name for p in Participant.query.all()}

    leaders = {}
    for stage_id, rider_pts in stage_rider_pts.items():
        best = 0
        names = []
        for pid, rider_ids in part_geel.items():
            pts = sum(rider_pts.get(rid, 0) for rid in rider_ids)
            if pts > best:
                best, names = pts, [part_name.get(pid, '?')]
            elif pts == best and pts > 0:
                names.append(part_name.get(pid, '?'))
        if best > 0:
            leaders[stage_id] = {'points': best, 'names': sorted(names)}
    return leaders


def get_last_stage_deltas():
    """Bereken voor de meest recente etappe hoeveel geel-punten elke deelnemer
    erbij kreeg. Returnt (stage_number, {participant_id: punten}, hoogste_score).
    Als er nog geen etappe is: (None, {}, 0)."""
    from collections import defaultdict
    last_stage = Stage.query.order_by(Stage.number.desc()).first()
    if not last_stage:
        return None, {}, 0

    rider_pts = defaultdict(int)
    for r in StageResult.query.filter_by(stage_id=last_stage.id).all():
        rider_pts[r.rider_id] += POINTS_TABLE.get(r.position, 0)
    for jw in JerseyWearer.query.filter_by(stage_id=last_stage.id).all():
        rider_pts[jw.rider_id] += JERSEY_DAILY_POINTS

    part_geel = defaultdict(set)
    for s in Selection.query.filter_by(type='geel').all():
        part_geel[s.participant_id].add(s.rider_id)

    deltas = {}
    for pid, rider_ids in part_geel.items():
        deltas[pid] = sum(rider_pts.get(rid, 0) for rid in rider_ids)

    best = max(deltas.values()) if deltas else 0
    return last_stage.number, deltas, best


def annotate_last_stage(standings):
    """Voeg per deelnemer de dagscore van de laatste etappe toe ('last_delta')
    en markeer wie de hoogste dagscore heeft ('is_day_leader').
    Returnt het etappenummer (of None)."""
    stage_num, deltas, best = get_last_stage_deltas()
    for s in standings:
        d = deltas.get(s['participant'].id, 0)
        s['last_delta'] = d
        s['is_day_leader'] = (stage_num is not None and best > 0 and d == best)
    return stage_num


def get_participant_scores():
    """Return list of dicts with geel/rood scores per participant.
    Bonuspunten tellen pas mee zodra bonus_active() (na de laatste etappe)."""
    rider_points = get_rider_points_map()
    include_bonus = bonus_active()
    bonus_counts = {}
    if include_bonus:
        for ba in BonusAnswer.query.filter_by(correct=True).all():
            bonus_counts[ba.participant_id] = bonus_counts.get(ba.participant_id, 0) + 1

    participants = Participant.query.order_by(Participant.name).all()
    result = []
    for p in participants:
        geel_pts = sum(rider_points[s.rider_id]
                       for s in p.selections if s.type == 'geel')
        geel_pts += bonus_counts.get(p.id, 0) * BONUS_POINTS

        rood_entries = p.rood_entries
        has_rood = bool(rood_entries)
        rood_pts = (sum(rider_points.get(e.matched_rider_id, 0)
                        for e in rood_entries if e.matched_rider_id)
                    if rood_entries else None)

        result.append({
            'participant': p,
            'geel': geel_pts,
            'rood': rood_pts,
            'has_rood': has_rood,
        })
    return result


# ── Public routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    scores = get_participant_scores()
    geel_all = sorted(scores, key=lambda x: x['geel'], reverse=True)
    last_stage_num = annotate_last_stage(geel_all)
    geel = geel_all[:5]
    rood = sorted((s for s in scores if s['has_rood']), key=lambda x: x['rood'])[:5]
    stages_done = Stage.query.count()
    return render_template('index.html', geel=geel, rood=rood, stages_done=stages_done,
                           last_stage_num=last_stage_num)


@app.route('/geel')
def geel_klassement():
    scores = get_participant_scores()
    standings = sorted(scores, key=lambda x: x['geel'], reverse=True)
    for i, s in enumerate(standings):
        s['rank'] = i + 1
    last_stage_num = annotate_last_stage(standings)

    clusters = Cluster.query.order_by(Cluster.name).all()
    cluster_data = {}
    cluster_ranking = []
    for c in clusters:
        members = [s for s in standings if s['participant'].cluster_id == c.id]
        if members:
            cluster_data[c.name] = members
            total = sum(s['geel'] for s in members)
            cluster_ranking.append({
                'name': c.name,
                'count': len(members),
                'total': total,
                'avg': total / len(members),
            })

    # Ranglijst op gemiddelde gele score (hoogste gemiddelde eerst), tie-bewust
    cluster_ranking.sort(key=lambda x: x['avg'], reverse=True)
    for i, cr in enumerate(cluster_ranking):
        if i > 0 and cr['avg'] == cluster_ranking[i - 1]['avg']:
            cr['rank'] = cluster_ranking[i - 1]['rank']
        else:
            cr['rank'] = i + 1

    unclustered = [s for s in standings if s['participant'].cluster_id is None]

    return render_template('geel.html', standings=standings,
                           cluster_data=cluster_data, unclustered=unclustered,
                           cluster_ranking=cluster_ranking,
                           last_stage_num=last_stage_num)


@app.route('/rood')
def rood_klassement():
    scores = get_participant_scores()
    standings = sorted((s for s in scores if s['has_rood']), key=lambda x: x['rood'])
    # Tie-bewuste ranking: gelijke scores delen dezelfde rang (1, 1, 3, …)
    for i, s in enumerate(standings):
        if i > 0 and s['rood'] == standings[i - 1]['rood']:
            s['rank'] = standings[i - 1]['rank']
        else:
            s['rank'] = i + 1
    # Iedereen met de laagste score is (gedeeld) rode lantaarn
    min_rood = standings[0]['rood'] if standings else None
    for s in standings:
        s['is_lantaarn'] = (min_rood is not None and s['rood'] == min_rood)
    return render_template('rood.html', standings=standings)


@app.route('/eindstand')
def eindstand():
    scores = get_participant_scores()
    geel = sorted(scores, key=lambda x: x['geel'], reverse=True)
    for i, s in enumerate(geel):
        s['geel_rank'] = i + 1
    rood = sorted((s for s in scores if s['has_rood']), key=lambda x: x['rood'])
    for i, s in enumerate(rood):
        s['rood_rank'] = i + 1
    stages_done = Stage.query.count()
    status = eindstand_status()
    return render_template('eindstand.html', geel=geel, rood=rood,
                           stages_done=stages_done, total_stages=TOTAL_STAGES,
                           is_final=status['is_final'], visible=status['visible'],
                           published=status['published'], is_admin=status['is_admin'],
                           admin_preview=status['admin_preview'])


@app.route('/eindstand/publiceren', methods=['POST'])
@require_admin
def publiceer_eindstand():
    action = request.form.get('action')
    if action == 'publish':
        set_setting('eindstand_published', '1')
        flash('De eindstand is nu zichtbaar voor alle deelnemers.', 'success')
    else:
        set_setting('eindstand_published', '0')
        flash('De eindstand is weer verborgen voor deelnemers.', 'warning')
    return redirect(url_for('eindstand'))


@app.route('/deelnemer/<int:pid>')
def deelnemer(pid):
    p = Participant.query.get_or_404(pid)
    rider_points = get_rider_points_map()

    geel_team = sorted(
        [{'rider': s.rider, 'points': rider_points[s.rider_id]}
         for s in p.selections if s.type == 'geel'],
        key=lambda x: x['points'], reverse=True
    )

    rood_entries_raw = RoodEntry.query.filter_by(participant_id=pid)\
        .order_by(RoodEntry.position).all()
    rood_team = []
    for e in rood_entries_raw:
        pts = rider_points.get(e.matched_rider_id, 0) if e.matched_rider_id else 0
        niet_gestart = e.matched_rider.niet_gestart if e.matched_rider else False
        rood_team.append({
            'display_name': e.matched_rider.name if e.matched_rider else e.custom_name,
            'custom_name': e.custom_name,
            'matched': bool(e.matched_rider_id),
            'niet_gestart': niet_gestart,
            'points': pts,
        })
    rood_team.sort(key=lambda x: x['points'])

    geel_total = sum(x['points'] for x in geel_team)
    show_bonus = bonus_active()
    if show_bonus:
        bonus_correct = BonusAnswer.query.filter_by(participant_id=pid, correct=True).count()
        geel_total += bonus_correct * BONUS_POINTS

    rood_total = sum(x['points'] for x in rood_team) if rood_team else None

    questions = BonusQuestion.query.order_by(BonusQuestion.number).all()
    answers = {ba.question_id: ba.correct
               for ba in BonusAnswer.query.filter_by(participant_id=pid).all()}

    all_participants = Participant.query.order_by(Participant.name).all()
    stage_points = get_participant_stage_points(pid)
    stage_breakdown = get_participant_stage_breakdown(pid)
    teams_visible = now_nl() >= INSCHRIJF_DEADLINE

    return render_template('deelnemer.html', p=p, geel_team=geel_team,
                           rood_team=rood_team, geel_total=geel_total,
                           rood_total=rood_total, questions=questions,
                           answers=answers, participants=all_participants,
                           show_bonus=show_bonus,
                           stage_points=stage_points,
                           stage_breakdown=stage_breakdown,
                           teams_visible=teams_visible)


@app.route('/etappes')
def etappes():
    stages = Stage.query.order_by(Stage.number).all()
    day_leaders = get_stage_day_leaders()
    stage_data = []
    for stage in stages:
        jerseys = {jw.jersey_type: jw.rider for jw in stage.jersey_wearers}
        stage_data.append({'stage': stage, 'results': stage.results, 'jerseys': jerseys,
                           'day_leader': day_leaders.get(stage.id)})
    return render_template('etappes.html', stage_data=stage_data,
                           points_table=POINTS_TABLE, jersey_labels=JERSEY_LABELS)


@app.route('/renners')
def renners():
    from collections import Counter
    rider_points = get_rider_points_map()
    geel_counts = Counter(s.rider_id for s in Selection.query.filter_by(type='geel').all())
    rood_counts  = Counter(e.matched_rider_id for e in RoodEntry.query.all()
                           if e.matched_rider_id)
    n_participants = Participant.query.count() or 1

    riders = Rider.query.order_by(Rider.name).all()
    data = []
    for r in riders:
        gc = geel_counts.get(r.id, 0)
        rc = rood_counts.get(r.id, 0)
        pts = rider_points.get(r.id, 0)
        if gc > 0 or rc > 0 or pts > 0:
            data.append({
                'rider': r,
                'points': pts,
                'geel_count': gc,
                'rood_count': rc,
                'geel_pct': round(gc / n_participants * 100),
                'rood_pct': round(rc / n_participants * 100),
            })
    data.sort(key=lambda x: x['points'], reverse=True)
    teams_visible = now_nl() >= INSCHRIJF_DEADLINE
    return render_template('renners.html', rider_data=data, n_participants=n_participants,
                           teams_visible=teams_visible)


@app.route('/api/chart/geel')
def api_chart_geel():
    """Return cumulative geel-scores per stage per participant as JSON for Chart.js."""
    from collections import defaultdict
    stages = Stage.query.order_by(Stage.number).all()
    if not stages:
        return {'labels': [], 'datasets': []}

    participants = Participant.query.order_by(Participant.name).all()

    # Build rider -> set of stages where they scored, cumulative
    # rider_id -> {stage_id: points_in_that_stage}
    stage_rider_pts = defaultdict(lambda: defaultdict(int))
    for sr in StageResult.query.all():
        stage_rider_pts[sr.stage_id][sr.rider_id] += POINTS_TABLE.get(sr.position, 0)
    for jw in JerseyWearer.query.all():
        stage_rider_pts[jw.stage_id][jw.rider_id] += JERSEY_DAILY_POINTS

    # Bonus points per participant (pas actief na de laatste etappe, added to last stage)
    bonus_counts = {}
    if bonus_active():
        for ba in BonusAnswer.query.filter_by(correct=True).all():
            bonus_counts[ba.participant_id] = bonus_counts.get(ba.participant_id, 0) + 1

    labels = [f'E{s.number}' for s in stages]
    datasets = []

    # Colour palette cycling
    COLOURS = ['#FFD700','#e63946','#2a9d2f','#457b9d','#f4a261',
               '#a8dadc','#6a4c93','#ff6b6b','#51cf66','#339af0',
               '#f06595','#ffa94d','#63e6be','#74c0fc','#da77f2',
               '#a9e34b','#ffec99','#99e9f2','#eebefa','#8ce99a']

    for i, p in enumerate(participants):
        geel_rider_ids = {s.rider_id for s in p.selections if s.type == 'geel'}
        if not geel_rider_ids:
            continue
        cumulative = 0
        points_per_stage = []
        for s in stages:
            for rid in geel_rider_ids:
                cumulative += stage_rider_pts[s.id].get(rid, 0)
            points_per_stage.append(cumulative)

        # Add bonus to final stage value
        if bonus_counts.get(p.id):
            points_per_stage[-1] += bonus_counts[p.id] * BONUS_POINTS

        colour = COLOURS[i % len(COLOURS)]
        datasets.append({
            'label': p.name,
            'data': points_per_stage,
            'borderColor': colour,
            'backgroundColor': colour + '33',
            'tension': 0.3,
            'pointRadius': 3,
        })

    # Sort datasets by final score descending for legend order
    datasets.sort(key=lambda d: d['data'][-1] if d['data'] else 0, reverse=True)
    from flask import jsonify
    return jsonify({'labels': labels, 'datasets': datasets})


@app.route('/api/chart/positie')
def api_chart_positie():
    """Return rank per stage per participant as JSON for Chart.js."""
    from collections import defaultdict
    from flask import jsonify
    stages = Stage.query.order_by(Stage.number).all()
    if not stages:
        return jsonify({'labels': [], 'datasets': []})

    participants = Participant.query.order_by(Participant.name).all()

    stage_rider_pts = defaultdict(lambda: defaultdict(int))
    for sr in StageResult.query.all():
        stage_rider_pts[sr.stage_id][sr.rider_id] += POINTS_TABLE.get(sr.position, 0)
    for jw in JerseyWearer.query.all():
        stage_rider_pts[jw.stage_id][jw.rider_id] += JERSEY_DAILY_POINTS

    bonus_counts = {}
    if bonus_active():
        for ba in BonusAnswer.query.filter_by(correct=True).all():
            bonus_counts[ba.participant_id] = bonus_counts.get(ba.participant_id, 0) + 1

    # Compute cumulative totals per participant per stage
    p_cum = {p.id: 0 for p in participants}
    p_rider_ids = {p.id: {s.rider_id for s in p.selections if s.type == 'geel'}
                   for p in participants}

    labels = [f'E{s.number}' for s in stages]
    p_scores_over_time = {p.id: [] for p in participants}

    for si, stage in enumerate(stages):
        for p in participants:
            for rid in p_rider_ids[p.id]:
                p_cum[p.id] += stage_rider_pts[stage.id].get(rid, 0)
        # Rank at this stage
        totals = []
        for p in participants:
            extra = bonus_counts.get(p.id, 0) * BONUS_POINTS if si == len(stages) - 1 else 0
            totals.append((p.id, p_cum[p.id] + extra))
        totals.sort(key=lambda x: x[1], reverse=True)
        rank_map = {pid: rank + 1 for rank, (pid, _) in enumerate(totals)}
        for p in participants:
            p_scores_over_time[p.id].append(rank_map[p.id])

    COLOURS = ['#FFD700','#e63946','#2a9d2f','#457b9d','#f4a261',
               '#a8dadc','#6a4c93','#ff6b6b','#51cf66','#339af0',
               '#f06595','#ffa94d','#63e6be','#74c0fc','#da77f2',
               '#a9e34b','#ffec99','#99e9f2','#eebefa','#8ce99a']

    datasets = []
    for i, p in enumerate(participants):
        if not p_rider_ids[p.id]:
            continue
        colour = COLOURS[i % len(COLOURS)]
        datasets.append({
            'label': p.name,
            'data': p_scores_over_time[p.id],
            'borderColor': colour,
            'backgroundColor': colour + '33',
            'tension': 0.3,
            'pointRadius': 3,
        })
    # Sort by final rank
    datasets.sort(key=lambda d: d['data'][-1] if d['data'] else 999)
    return jsonify({'labels': labels, 'datasets': datasets})


# ── Inschrijving ───────────────────────────────────────────────────────────────

@app.route('/inschrijven', methods=['GET', 'POST'])
def inschrijven():
    riders = Rider.query.order_by(Rider.name).all()
    questions = BonusQuestion.query.order_by(BonusQuestion.number).all()
    now = now_nl()
    gesloten = now > INSCHRIJF_DEADLINE

    if request.method == 'POST':
        if gesloten:
            flash('De inschrijving is gesloten.', 'danger')
            return redirect(url_for('inschrijven'))

        naam = request.form.get('naam', '').strip()
        afdeling = request.form.get('afdeling', '').strip()
        geel_ids = [int(x) for x in request.form.getlist('geel_riders')]
        rood_ids = [int(x) for x in request.form.getlist('rood_riders')]

        # Validatie
        errors = []
        if not naam:
            errors.append('Voer je naam in.')
        if not afdeling:
            errors.append('Voer je afdeling in.')
        if len(geel_ids) != MAX_GEEL:
            errors.append(f'Kies precies {MAX_GEEL} renners voor je geel team (nu {len(geel_ids)}).')
        if rood_ids and len(rood_ids) != MAX_ROOD:
            errors.append(f'Kies precies {MAX_ROOD} renners voor je rood team of laat alles leeg (nu {len(rood_ids)}).')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return redirect(url_for('inschrijven'))

        if Participant.query.filter_by(name=naam).first():
            flash(f'Er is al een inschrijving op naam "{naam}". Neem contact op als dit een fout is.', 'warning')
            return redirect(url_for('inschrijven'))

        # Cluster/afdeling
        cluster_id = None
        if afdeling:
            c = Cluster.query.filter_by(name=afdeling).first()
            if not c:
                c = Cluster(name=afdeling)
                db.session.add(c)
                db.session.flush()
            cluster_id = c.id

        p = Participant(name=naam, cluster_id=cluster_id, edit_token=str(uuid.uuid4()))
        db.session.add(p)
        db.session.flush()

        for rid in geel_ids:
            db.session.add(Selection(participant_id=p.id, rider_id=rid, type='geel'))

        # Rood: geselecteerde renners vanuit de startlijst
        if rood_ids:
            riders_map = {r.id: r for r in Rider.query.filter(Rider.id.in_(rood_ids)).all()}
            for pos, rid in enumerate(rood_ids, 1):
                r = riders_map.get(rid)
                if r:
                    db.session.add(RoodEntry(
                        participant_id=p.id,
                        custom_name=r.name,
                        matched_rider_id=r.id,
                        position=pos,
                    ))

        for q in questions:
            answer_text = request.form.get(f'bonus_{q.id}', '').strip()
            if answer_text:
                db.session.add(BonusAnswer(
                    question_id=q.id, participant_id=p.id,
                    correct=False, answer_text=answer_text))

        db.session.commit()
        return redirect(url_for('inschrijven_bevestiging', naam=naam))

    # Group riders by team for grouped display; riders without team go under 'Overig'
    riders_by_team = {}
    for r in sorted(Rider.query.order_by(Rider.team, Rider.name).all(),
                    key=lambda r: (r.team or 'zzz_overig', r.name)):
        team_key = r.team or 'Overig'
        riders_by_team.setdefault(team_key, []).append(r)

    return render_template('inschrijven.html', riders=riders, riders_by_team=riders_by_team,
                           questions=questions, gesloten=gesloten, deadline=INSCHRIJF_DEADLINE,
                           inschrijfgeld=INSCHRIJFGELD, now=now,
                           max_geel=MAX_GEEL, max_rood=MAX_ROOD)


@app.route('/inschrijven/bevestiging')
def inschrijven_bevestiging():
    naam = request.args.get('naam', 'Deelnemer')
    p = Participant.query.filter_by(name=naam).first()
    return render_template('inschrijven_bevestiging.html', naam=naam, p=p)


@app.route('/mijn-team/<token>', methods=['GET', 'POST'])
def mijn_team(token):
    p = Participant.query.filter_by(edit_token=token).first_or_404()
    riders = Rider.query.order_by(Rider.name).all()
    questions = BonusQuestion.query.order_by(BonusQuestion.number).all()
    now = now_nl()
    gesloten = now > INSCHRIJF_DEADLINE

    if request.method == 'POST':
        if gesloten:
            flash('De wijzigingstermijn is gesloten.', 'danger')
            return redirect(url_for('mijn_team', token=token))

        afdeling = request.form.get('afdeling', '').strip()
        geel_ids = [int(x) for x in request.form.getlist('geel_riders')]
        rood_ids = [int(x) for x in request.form.getlist('rood_riders')]

        errors = []
        if len(geel_ids) != MAX_GEEL:
            errors.append(f'Kies precies {MAX_GEEL} renners voor je geel team (nu {len(geel_ids)}).')
        if rood_ids and len(rood_ids) != MAX_ROOD:
            errors.append(f'Kies precies {MAX_ROOD} renners voor je rood team of laat alles leeg (nu {len(rood_ids)}).')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return redirect(url_for('mijn_team', token=token))

        # Update afdeling/cluster
        if afdeling:
            c = Cluster.query.filter_by(name=afdeling).first()
            if not c:
                c = Cluster(name=afdeling)
                db.session.add(c)
                db.session.flush()
            p.cluster_id = c.id

        # Replace geel selections
        Selection.query.filter_by(participant_id=p.id, type='geel').delete()
        for rid in geel_ids:
            db.session.add(Selection(participant_id=p.id, rider_id=rid, type='geel'))

        # Replace rood entries
        RoodEntry.query.filter_by(participant_id=p.id).delete()
        if rood_ids:
            riders_map = {r.id: r for r in Rider.query.filter(Rider.id.in_(rood_ids)).all()}
            for pos, rid in enumerate(rood_ids, 1):
                r = riders_map.get(rid)
                if r:
                    db.session.add(RoodEntry(
                        participant_id=p.id,
                        custom_name=r.name,
                        matched_rider_id=r.id,
                        position=pos,
                    ))

        # Replace bonus answers (reset correct=False; admin re-evaluates)
        BonusAnswer.query.filter_by(participant_id=p.id).delete()
        for q in questions:
            answer_text = request.form.get(f'bonus_{q.id}', '').strip()
            if answer_text:
                db.session.add(BonusAnswer(
                    question_id=q.id, participant_id=p.id,
                    correct=False, answer_text=answer_text))

        db.session.commit()
        flash('Je team is bijgewerkt!', 'success')
        return redirect(url_for('deelnemer', pid=p.id))

    # GET: build pre-filled context
    riders_by_team = {}
    for r in sorted(Rider.query.order_by(Rider.team, Rider.name).all(),
                    key=lambda r: (r.team or 'zzz_overig', r.name)):
        team_key = r.team or 'Overig'
        riders_by_team.setdefault(team_key, []).append(r)

    current_geel_ids = {s.rider_id for s in p.selections if s.type == 'geel'}
    current_rood_ids = {e.matched_rider_id for e in p.rood_entries if e.matched_rider_id}
    current_bonus = {ba.question_id: (ba.answer_text or '') for ba in p.bonus_answers}

    return render_template('inschrijven.html',
                           riders=riders, riders_by_team=riders_by_team,
                           questions=questions, gesloten=gesloten, deadline=INSCHRIJF_DEADLINE,
                           inschrijfgeld=INSCHRIJFGELD, now=now,
                           max_geel=MAX_GEEL, max_rood=MAX_ROOD,
                           edit_mode=True, edit_token=token, participant=p,
                           current_geel_ids=current_geel_ids,
                           current_rood_ids=current_rood_ids,
                           current_bonus=current_bonus)


# ── Admin routes ───────────────────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_index'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == app.config['ADMIN_PASSWORD']:
            session['admin_logged_in'] = True
            session.permanent = True
            next_url = request.form.get('next') or url_for('admin_index')
            return redirect(next_url)
        flash('Ongeldig wachtwoord.', 'danger')
    return render_template('admin/login.html', next=request.args.get('next', ''))


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    flash('Je bent uitgelogd.', 'info')
    return redirect(url_for('index'))


@app.route('/admin/handleiding')
@require_admin
def admin_handleiding():
    return render_template('admin/handleiding.html',
                           points_table=POINTS_TABLE,
                           bonus_points=BONUS_POINTS,
                           max_geel=MAX_GEEL,
                           max_rood=MAX_ROOD,
                           deadline=INSCHRIJF_DEADLINE)


@app.route('/admin/export')
@require_admin
def admin_export():
    import io
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')

    # Header
    geel_cols = [f'Geel {i}' for i in range(1, MAX_GEEL + 1)]
    rood_cols = [f'Rood {i}' for i in range(1, MAX_ROOD + 1)]
    writer.writerow(['Naam', 'Afdeling'] + geel_cols + rood_cols)

    participants = Participant.query.order_by(Participant.name).all()
    for p in participants:
        geel = [s.rider.name for s in
                sorted(p.selections, key=lambda s: s.rider.name)
                if s.type == 'geel']
        rood = [e.custom_name for e in
                sorted(p.rood_entries, key=lambda e: e.position or 0)]
        # Pad to fixed width
        geel += [''] * (MAX_GEEL - len(geel))
        rood += [''] * (MAX_ROOD - len(rood))
        afdeling = p.cluster.name if p.cluster else ''
        writer.writerow([p.name, afdeling] + geel + rood)

    output.seek(0)
    from flask import Response
    return Response(
        '﻿' + output.getvalue(),  # BOM for Excel UTF-8
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=tourpoule_deelnemers.csv'}
    )


@app.route('/admin')
@require_admin
def admin_index():
    # Build niet-gestart warnings
    niet_gestart_warnings = []
    for r in Rider.query.filter_by(niet_gestart=True).all():
        geel_aff = [s.participant for s in
                    Selection.query.filter_by(rider_id=r.id, type='geel').all()]
        rood_aff = [e.participant for e in
                    RoodEntry.query.filter_by(matched_rider_id=r.id).all()]
        if geel_aff or rood_aff:
            niet_gestart_warnings.append({
                'rider': r,
                'geel': geel_aff,
                'rood': rood_aff,
            })

    return render_template('admin/index.html',
                           n_participants=Participant.query.count(),
                           n_riders=Rider.query.count(),
                           n_stages=Stage.query.count(),
                           n_questions=BonusQuestion.query.count(),
                           niet_gestart_warnings=niet_gestart_warnings)


@app.route('/admin/deelnemers', methods=['GET', 'POST'])
@require_admin
def admin_deelnemers():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_cluster':
            name = request.form.get('cluster_name', '').strip()
            if name and not Cluster.query.filter_by(name=name).first():
                db.session.add(Cluster(name=name))
                db.session.commit()
                flash(f'Cluster "{name}" toegevoegd.', 'success')
        elif action == 'add_participant':
            name = request.form.get('name', '').strip()
            cluster_id = request.form.get('cluster_id') or None
            if name and not Participant.query.filter_by(name=name).first():
                db.session.add(Participant(name=name, cluster_id=cluster_id))
                db.session.commit()
                flash(f'{name} toegevoegd.', 'success')
            elif Participant.query.filter_by(name=name).first():
                flash(f'{name} bestaat al.', 'warning')
        elif action == 'bulk_add':
            names = request.form.get('names', '')
            cluster_id = request.form.get('cluster_id') or None
            added = 0
            for name in names.strip().splitlines():
                name = name.strip()
                if name and not Participant.query.filter_by(name=name).first():
                    db.session.add(Participant(name=name, cluster_id=cluster_id))
                    added += 1
            db.session.commit()
            flash(f'{added} deelnemers toegevoegd.', 'success')
        elif action == 'change_cluster':
            pid = request.form.get('participant_id')
            cluster_id = request.form.get('cluster_id') or None
            p = Participant.query.get(pid)
            if p:
                p.cluster_id = cluster_id
                db.session.commit()
                flash(f'Afdeling van {p.name} aangepast.', 'success')
        elif action == 'rename_cluster':
            cid = request.form.get('cluster_id_rename')
            new_name = request.form.get('new_name', '').strip()
            c = Cluster.query.get(cid)
            if c and new_name:
                existing = Cluster.query.filter_by(name=new_name).first()
                if existing and existing.id != c.id:
                    # Samenvoegen: verplaats deelnemers naar bestaande cluster, verwijder deze.
                    # Via de relationship (p.cluster) zodat de FK niet weer op NULL wordt gezet.
                    for p in list(c.participants):
                        p.cluster = existing
                    db.session.flush()
                    db.session.delete(c)
                    db.session.commit()
                    flash(f'Cluster "{c.name}" samengevoegd met "{new_name}".', 'success')
                else:
                    old = c.name
                    c.name = new_name
                    db.session.commit()
                    flash(f'Cluster "{old}" hernoemd naar "{new_name}".', 'success')
        elif action == 'delete_participant':
            pid = request.form.get('participant_id')
            p = Participant.query.get(pid)
            if p:
                db.session.delete(p)
                db.session.commit()
                flash(f'{p.name} verwijderd.', 'warning')
        elif action == 'delete_cluster':
            cid = request.form.get('cluster_id_del')
            c = Cluster.query.get(cid)
            if c:
                db.session.delete(c)
                db.session.commit()
                flash(f'Cluster "{c.name}" verwijderd.', 'warning')
        return redirect(url_for('admin_deelnemers'))

    participants = Participant.query.order_by(Participant.name).all()
    clusters = Cluster.query.order_by(Cluster.name).all()
    return render_template('admin/deelnemers.html', participants=participants, clusters=clusters)


@app.route('/admin/renners', methods=['GET', 'POST'])
@require_admin
def admin_renners():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            name = request.form.get('name', '').strip()
            team = request.form.get('team', '').strip() or None
            if name and not Rider.query.filter_by(name=name).first():
                db.session.add(Rider(name=name, team=team))
                db.session.commit()
                flash(f'{name} toegevoegd.', 'success')
        elif action == 'bulk_add':
            raw = request.form.get('names', '')
            added = 0
            parsed = _parse_names_from_pcs_html(raw)
            if not parsed:
                # Fallback: treat every non-empty line as a name with no team
                for line in raw.strip().splitlines():
                    line = line.strip()
                    if line:
                        parsed.append({'name': line, 'team': None})
            for entry in parsed:
                name = entry['name'].strip()
                team = entry.get('team') or None
                if name and not Rider.query.filter_by(name=name).first():
                    db.session.add(Rider(name=name, team=team))
                    added += 1
            db.session.commit()
            flash(f'{added} renners toegevoegd.', 'success')
        elif action == 'edit_team':
            rid = request.form.get('rider_id')
            team = request.form.get('team', '').strip() or None
            r = Rider.query.get(rid)
            if r:
                r.team = team
                db.session.commit()
                flash(f'Team van {r.name} aangepast naar "{team or "—"}".', 'success')
        elif action == 'delete':
            rid = request.form.get('rider_id')
            r = Rider.query.get(rid)
            if r:
                db.session.delete(r)
                db.session.commit()
                flash(f'{r.name} verwijderd.', 'warning')
        elif action == 'delete_all':
            count = Rider.query.count()
            Rider.query.delete()
            db.session.commit()
            flash(f'Alle {count} renners verwijderd.', 'warning')
        elif action == 'toggle_niet_gestart':
            rid = request.form.get('rider_id')
            r = Rider.query.get(rid)
            if r:
                r.niet_gestart = not r.niet_gestart
                db.session.commit()
                status = 'gemarkeerd als niet gestart' if r.niet_gestart else 'terug op actief'
                flash(f'{r.name} {status}.', 'warning' if r.niet_gestart else 'success')
        return redirect(url_for('admin_renners'))

    riders = Rider.query.order_by(Rider.name).all()
    return render_template('admin/renners.html', riders=riders)


@app.route('/admin/teams', methods=['GET', 'POST'])
@require_admin
def admin_teams():
    participants = Participant.query.order_by(Participant.name).all()
    riders = Rider.query.order_by(Rider.name).all()
    pid = request.args.get('pid', type=int) or (participants[0].id if participants else None)

    if request.method == 'POST':
        pid = int(request.form.get('participant_id'))
        action = request.form.get('action', 'save_geel')

        if action == 'save_geel':
            rider_ids = [int(x) for x in request.form.getlist('rider_ids')]
            if len(rider_ids) > MAX_GEEL:
                flash(f'Maximaal {MAX_GEEL} renners voor geel team.', 'danger')
            else:
                Selection.query.filter_by(participant_id=pid, type='geel').delete()
                for rid in rider_ids:
                    db.session.add(Selection(participant_id=pid, rider_id=rid, type='geel'))
                db.session.commit()
                flash('Geel team opgeslagen.', 'success')

        elif action == 'save_rood_entries':
            # Save free-text rood entries + optional matched_rider_id per entry
            RoodEntry.query.filter_by(participant_id=pid).delete()
            for i in range(1, MAX_ROOD + 1):
                cname = request.form.get(f'rood_name_{i}', '').strip()
                if not cname:
                    continue
                mid_raw = request.form.get(f'rood_match_{i}', '')
                mid = int(mid_raw) if mid_raw else None
                db.session.add(RoodEntry(
                    participant_id=pid,
                    custom_name=cname,
                    matched_rider_id=mid,
                    position=i,
                ))
            db.session.commit()
            flash('Rood team opgeslagen.', 'success')

        elif action == 'clear_rood':
            RoodEntry.query.filter_by(participant_id=pid).delete()
            db.session.commit()
            flash('Rood team gewist.', 'warning')

        return redirect(url_for('admin_teams', pid=pid))

    current = Participant.query.get_or_404(pid) if pid else None
    geel_ids = set()
    current_rood = []
    if current:
        geel_ids = {s.rider_id for s in
                    Selection.query.filter_by(participant_id=pid, type='geel').all()}
        current_rood = RoodEntry.query.filter_by(participant_id=pid)\
            .order_by(RoodEntry.position).all()

    return render_template('admin/teams.html', participants=participants, riders=riders,
                           current=current, geel_ids=geel_ids,
                           current_rood=current_rood,
                           max_geel=MAX_GEEL, max_rood=MAX_ROOD)


@app.route('/admin/scrape-etappe', methods=['GET', 'POST'])
@require_admin
def admin_scrape_etappe():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    scraped = None
    error = None
    stage_num = request.args.get('stage', type=int) or request.form.get('stage_num', type=int)
    riders = Rider.query.order_by(Rider.name).all()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'scrape':
            if not stage_num:
                error = 'Kies een etappenummer.'
            else:
                scraped, error = _scrape_stage_results(stage_num)

        elif action == 'import':
            stage_num = request.form.get('stage_num', type=int)
            stage = Stage.query.filter_by(number=stage_num).first()
            if not stage:
                stage = Stage(number=stage_num)
                db.session.add(stage)
                db.session.flush()

            StageResult.query.filter_by(stage_id=stage.id).delete()
            JerseyWearer.query.filter_by(stage_id=stage.id).delete()

            for pos in range(1, 16):
                rid = request.form.get(f'pos_{pos}', type=int)
                if rid:
                    db.session.add(StageResult(stage_id=stage.id, position=pos,
                                               rider_id=rid))
            for jersey in ('yellow', 'green', 'polka', 'white'):
                rid = request.form.get(f'jersey_{jersey}', type=int)
                if rid:
                    db.session.add(JerseyWearer(stage_id=stage.id,
                                                jersey_type=jersey, rider_id=rid))
            db.session.commit()
            flash(f'Etappe {stage_num} opgeslagen.', 'success')
            return redirect(url_for('admin_etappe', stage_num=stage_num))

    return render_template('admin/scrape_etappe.html',
                           scraped=scraped, error=error,
                           stage_num=stage_num, riders=riders,
                           jersey_labels=JERSEY_LABELS,
                           points_table=POINTS_TABLE)


@app.route('/admin/etappe', methods=['GET', 'POST'])
@app.route('/admin/etappe/<int:stage_num>', methods=['GET', 'POST'])
@require_admin
def admin_etappe(stage_num=None):
    riders = Rider.query.order_by(Rider.name).all()
    completed_stages = [s.number for s in Stage.query.order_by(Stage.number).all()]

    if request.method == 'POST':
        stage_num = int(request.form.get('stage_num'))
        stage = Stage.query.filter_by(number=stage_num).first()
        if not stage:
            stage = Stage(number=stage_num)
            db.session.add(stage)
            db.session.flush()

        StageResult.query.filter_by(stage_id=stage.id).delete()
        JerseyWearer.query.filter_by(stage_id=stage.id).delete()

        for pos in range(1, 16):
            rid = request.form.get(f'pos_{pos}')
            if rid:
                db.session.add(StageResult(stage_id=stage.id, position=pos, rider_id=int(rid)))

        for jersey in ('yellow', 'green', 'polka', 'white'):
            rid = request.form.get(f'jersey_{jersey}')
            if rid:
                db.session.add(JerseyWearer(stage_id=stage.id, jersey_type=jersey, rider_id=int(rid)))

        db.session.commit()
        flash(f'Etappe {stage_num} opgeslagen.', 'success')
        return redirect(url_for('admin_etappe', stage_num=stage_num))

    current_stage = Stage.query.filter_by(number=stage_num).first() if stage_num else None
    current_results = {}
    current_jerseys = {}
    if current_stage:
        current_results = {r.position: r.rider_id for r in current_stage.results}
        current_jerseys = {jw.jersey_type: jw.rider_id for jw in current_stage.jersey_wearers}

    return render_template('admin/etappe.html', riders=riders, stage_num=stage_num,
                           completed_stages=completed_stages, current_results=current_results,
                           current_jerseys=current_jerseys, points_table=POINTS_TABLE,
                           jersey_labels=JERSEY_LABELS)


@app.route('/admin/eindklassement', methods=['GET', 'POST'])
@require_admin
def admin_eindklassement():
    riders = Rider.query.order_by(Rider.name).all()

    if request.method == 'POST':
        jersey_type = request.form.get('jersey_type')
        FinalClassification.query.filter_by(jersey_type=jersey_type).delete()
        for pos in range(1, 16):
            rid = request.form.get(f'pos_{pos}')
            if rid:
                db.session.add(FinalClassification(jersey_type=jersey_type,
                                                   position=pos, rider_id=int(rid)))
        db.session.commit()
        flash(f'Eindklassement {JERSEY_LABELS.get(jersey_type, jersey_type)} opgeslagen.', 'success')
        return redirect(url_for('admin_eindklassement'))

    current = {}
    for jersey in ('yellow', 'green', 'polka', 'white'):
        current[jersey] = {fc.position: fc.rider_id
                           for fc in FinalClassification.query.filter_by(jersey_type=jersey).all()}

    return render_template('admin/eindklassement.html', riders=riders, current=current,
                           jersey_labels=JERSEY_LABELS, points_table=POINTS_TABLE)


@app.route('/admin/bonusvragen', methods=['GET', 'POST'])
@require_admin
def admin_bonusvragen():
    participants = Participant.query.order_by(Participant.name).all()

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_question':
            q_text = request.form.get('question', '').strip()
            if q_text:
                q_num = (db.session.query(db.func.max(BonusQuestion.number)).scalar() or 0) + 1
                db.session.add(BonusQuestion(number=q_num, question=q_text))
                db.session.commit()
                flash(f'Bonusvraag {q_num} toegevoegd.', 'success')
        elif action == 'save_answers':
            qid = int(request.form.get('question_id'))
            for p in participants:
                correct = f'correct_{p.id}' in request.form
                ba = BonusAnswer.query.filter_by(question_id=qid, participant_id=p.id).first()
                if ba:
                    ba.correct = correct
                else:
                    db.session.add(BonusAnswer(question_id=qid, participant_id=p.id, correct=correct))
            db.session.commit()
            flash('Antwoorden opgeslagen.', 'success')
        elif action == 'delete_question':
            qid = int(request.form.get('question_id'))
            q = BonusQuestion.query.get(qid)
            if q:
                db.session.delete(q)
                db.session.commit()
                flash('Vraag verwijderd.', 'warning')
        return redirect(url_for('admin_bonusvragen'))

    questions = BonusQuestion.query.order_by(BonusQuestion.number).all()
    answers = {}       # qid -> {pid: correct_bool}
    answer_texts = {}  # qid -> {pid: answer_text}
    for q in questions:
        answers[q.id] = {}
        answer_texts[q.id] = {}
        for ba in BonusAnswer.query.filter_by(question_id=q.id).all():
            answers[q.id][ba.participant_id] = ba.correct
            answer_texts[q.id][ba.participant_id] = ba.answer_text or ''

    return render_template('admin/bonusvragen.html', questions=questions,
                           participants=participants, answers=answers,
                           answer_texts=answer_texts,
                           bonus_points=BONUS_POINTS)


# ── Name matching helpers ──────────────────────────────────────────────────────

def normalize_name(name):
    """Lowercase, strip accents, collapse whitespace."""
    name = unicodedata.normalize('NFKD', name)
    name = ''.join(c for c in name if not unicodedata.combining(c))
    return ' '.join(name.lower().split())


def build_rider_index(riders):
    """Map every normalized variant -> rider (handles LAST First / First LAST)."""
    index = {}
    lastname_index = {}  # normalized last name -> [riders] for single-match lookup
    for r in riders:
        norm = normalize_name(r.name)
        index[norm] = r
        parts = norm.split()
        if len(parts) >= 2:
            rev = ' '.join(reversed(parts))
            index.setdefault(rev, r)
            # Index first word as last name (our format is LASTNAME Firstname)
            lastname_index.setdefault(parts[0], []).append(r)
    return index, lastname_index


def match_rider_name(raw, rider_index, threshold=80):
    """
    Return (rider_id_or_None, score, auto_matched).
    score == 100 means exact (after normalization).
    auto_matched is True when score >= threshold.
    Handles: LAST First, First LAST, LAST only, First only, typos, missing accents.
    """
    if not raw or not raw.strip():
        return None, 0, False

    index, lastname_index = rider_index
    norm = normalize_name(raw)

    # 1. Exact normalized match
    if norm in index:
        return index[norm].id, 100, True

    # 2. Single-word input: try as last name (unique match only)
    parts = norm.split()
    if len(parts) == 1:
        candidates = lastname_index.get(norm, [])
        if len(candidates) == 1:
            return candidates[0].id, 100, True
        # Fuzzy match against last names only
        ln_result = fuzz_process.extractOne(norm, list(lastname_index.keys()),
                                            scorer=fuzz.ratio)
        if ln_result:
            ln_key, ln_score, _ = ln_result
            candidates = lastname_index.get(ln_key, [])
            if ln_score >= threshold and len(candidates) == 1:
                return candidates[0].id, int(ln_score), True

    # 3. Full fuzzy match (handles word-order differences, missing accents, typos)
    result = fuzz_process.extractOne(norm, list(index.keys()),
                                     scorer=fuzz.token_sort_ratio)
    if result:
        best_key, score, _ = result
        score = int(score)
        rider = index[best_key]
        return rider.id, score, score >= threshold

    return None, 0, False


PCS_STARTLIST_URL = 'https://www.procyclingstats.com/race/tour-de-france/2026/startlist'
PCS_RACE_PATH = 'race/tour-de-france/2026'
PCS_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}


def _scrape_stage_results(stage_num):
    """Fetch top-15 stage results from PCS.

    Returns (results, error) where results is a list of dicts:
    [{'pos': int, 'pcs_name': str, 'rider_id': int|None, 'match_score': int, 'auto': bool}]
    """
    import re
    import requests
    from bs4 import BeautifulSoup

    url = (f'https://www.procyclingstats.com/{PCS_RACE_PATH}'
           f'/stage-{stage_num}/result/result')

    try:
        resp = requests.get(url, headers=PCS_HEADERS, timeout=12,
                            verify=False, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else '?'
        return None, f'HTTP {code} bij ophalen van {url}'
    except requests.exceptions.RequestException as e:
        return None, str(e)

    soup = BeautifulSoup(resp.text, 'html.parser')
    results_table = soup.select_one('table.results')
    if not results_table:
        return None, 'Geen resultaten-tabel gevonden op PCS. Zijn de uitslag al gepubliceerd?'

    headers_row = [th.text.strip() for th in results_table.find_all('th')]
    all_riders = Rider.query.order_by(Rider.name).all()
    r_index = build_rider_index(all_riders)

    results = []
    for tr in results_table.select('tbody tr'):
        cols = tr.find_all('td')
        if len(cols) != len(headers_row):
            continue
        row = {}
        for i, td in enumerate(cols):
            h = headers_row[i]
            if h == 'Rider':
                links = td.find_all('a')
                row['pcs_name'] = (' '.join(links[0].stripped_strings)
                                   if links else td.get_text(' ', strip=True))
            elif h == 'Rnk':
                row['rnk'] = td.get_text(strip=True)

        rnk = row.get('rnk', '')
        if not re.match(r'^\d+$', rnk):
            continue  # skip DNF/OTL/DNS rows
        pos = int(rnk)
        if pos > 15:
            break

        pcs_name = row.get('pcs_name', '')
        rid, score, auto = match_rider_name(pcs_name, r_index)
        results.append({
            'pos': pos,
            'pcs_name': pcs_name,
            'rider_id': rid,
            'match_score': score,
            'auto': auto,
        })

    if not results:
        return None, 'Geen geldige posities gevonden in de tabel.'

    return results, None


def _clean_rider_name(name):
    """Normalise a scraped rider name.

    PCS marks young-classification riders (witte trui) with a trailing '*'
    (e.g. 'DEL TORO Isaac*'). Strip asterisks and collapse whitespace so the
    name matches the database and doesn't create duplicates on import.
    """
    import re
    name = name.replace('*', '')
    return re.sub(r'\s+', ' ', name).strip()


def _parse_names_from_pcs_html(html_or_text):
    """Extract riders from PCS HTML or plain text.

    Returns list of dicts: [{'name': str, 'team': str|None}]
    """
    import re
    from bs4 import BeautifulSoup

    riders = []
    seen = set()

    # ── HTML path (most reliable) ─────────────────────────────────────────────
    if '<html' in html_or_text.lower() or '<a ' in html_or_text.lower():
        soup = BeautifulSoup(html_or_text, 'html.parser')

        # PCS structure: ul.startlist_v4 > li.team > (team name in b/span) + ul > li > a[href*=/rider/]
        team_lis = soup.select('ul.startlist_v4 > li')
        if team_lis:
            for team_li in team_lis:
                # Team name is usually in the first <b> or <a> that is NOT a /rider/ link
                team_name = None
                for tag in team_li.find_all(['b', 'a'], recursive=False):
                    txt = tag.get_text(strip=True)
                    if txt and '/rider/' not in (tag.get('href') or ''):
                        team_name = txt
                        break
                if team_name is None:
                    b = team_li.find('b')
                    team_name = b.get_text(strip=True) if b else None

                for a in team_li.select('a[href*="/rider/"]'):
                    name = _clean_rider_name(a.get_text(strip=True))
                    if name and 3 < len(name) < 60 and ' ' in name and name not in seen:
                        riders.append({'name': name, 'team': team_name})
                        seen.add(name)

        # Fallback: flat rider link scan without team info
        if not riders:
            for a in soup.find_all('a', href=True):
                if '/rider/' in a['href']:
                    name = _clean_rider_name(a.get_text(strip=True))
                    if name and 3 < len(name) < 60 and ' ' in name and name not in seen:
                        riders.append({'name': name, 'team': None})
                        seen.add(name)

    # ── Plain text path ───────────────────────────────────────────────────────
    if not riders:
        current_team = None
        for line in html_or_text.splitlines():
            line = line.strip()
            if not line:
                continue

            # Strip leading hyphen/dash (PCS plain-text paste: "- LASTNAME Firstname")
            if line.startswith('-'):
                line = line.lstrip('-').strip()

            # Skip directeur sportif / staff lines (DS LASTNAME Firstname)
            if re.match(r'^DS\s+[A-Z]', line):
                continue

            # Strip trailing PCS timestamp like "20h", "3d", "1m" (often glued to last word or
            # separated by whitespace depending on browser/OS copy behaviour)
            line = re.sub(r'\s+\d+[hmd]\b.*$', '', line).strip()

            # Strip leading bib number
            line = re.sub(r'^\d+\s+', '', line).strip()
            if not line:
                continue

            parts = line.split()
            if len(parts) < 2 or len(parts) > 8:
                # Single-word or very long lines → skip, but don't use as team name
                continue

            # Lastname = leading all-caps tokens, firstname = rest
            lastname_parts, firstname_parts, in_last = [], [], True
            for p in parts:
                # Strip trailing time suffixes that may be glued to a token ("Søren20h" → "Søren")
                p = re.sub(r'\d+[hmd]$', '', p)
                if not p:
                    continue
                clean = p.replace('-', '').replace("'", '').replace('\u2019', '')
                if in_last and clean.isalpha() and clean == clean.upper() and len(clean) >= 2:
                    lastname_parts.append(p)
                else:
                    in_last = False
                    # Skip pure-digit leftover tokens in the firstname position
                    if not re.fullmatch(r'\d+', p):
                        firstname_parts.append(p)

            if lastname_parts and firstname_parts:
                # Skip team abbreviations like "UAE Team Emirates" (single ≤3-char token)
                if len(lastname_parts) == 1 and len(lastname_parts[0]) <= 3:
                    # Treat remainder as team name
                    current_team = line
                    continue
                name = _clean_rider_name(' '.join(lastname_parts) + ' ' + ' '.join(firstname_parts))
                if name and name not in seen and len(name) < 60:
                    riders.append({'name': name, 'team': current_team})
                    seen.add(name)
            else:
                # No rider pattern → treat as team name for subsequent lines
                current_team = line

    return riders


def _diff_startlist(riders_found):
    """Compare scraped riders against DB. Returns (scraped_list, to_remove_list)."""
    scraped_names = {r['name'] for r in riders_found}
    all_db = Rider.query.order_by(Rider.name).all()
    existing_map = {r.name: r for r in all_db}

    scraped = []
    for r in riders_found:
        db_rider = existing_map.get(r['name'])
        team_changed = db_rider and db_rider.team != r['team']
        scraped.append({
            'name': r['name'],
            'team': r['team'],
            'new': db_rider is None,
            'team_changed': team_changed,
            'old_team': db_rider.team if db_rider else None,
        })

    to_remove = []
    for r in all_db:
        if r.name not in scraped_names:
            geel_count = Selection.query.filter_by(rider_id=r.id, type='geel').count()
            rood_count = RoodEntry.query.filter_by(matched_rider_id=r.id).count()
            to_remove.append({
                'id': r.id,
                'name': r.name,
                'team': r.team,
                'geel_count': geel_count,
                'rood_count': rood_count,
                'has_selections': bool(geel_count or rood_count),
            })

    return scraped, to_remove


@app.route('/admin/scrape-startlist', methods=['GET', 'POST'])
@require_admin
def admin_scrape_startlist():
    import requests

    scraped = None
    to_remove = []
    error = None
    show_paste = False

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'scrape':
            try:
                sess = requests.Session()
                resp = sess.get(
                    PCS_STARTLIST_URL,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                                      'Chrome/124.0.0.0 Safari/537.36',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,'
                                  'image/avif,image/webp,*/*;q=0.8',
                        'Accept-Language': 'nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7',
                        'Accept-Encoding': 'gzip, deflate, br',
                        'Connection': 'keep-alive',
                        'Upgrade-Insecure-Requests': '1',
                        'Sec-Fetch-Dest': 'document',
                        'Sec-Fetch-Mode': 'navigate',
                        'Sec-Fetch-Site': 'none',
                        'Sec-Fetch-User': '?1',
                    },
                    timeout=12,
                    allow_redirects=True,
                )
                resp.raise_for_status()
                riders_found = _parse_names_from_pcs_html(resp.text)
                if not riders_found:
                    error = 'Geen renners gevonden. De startlijst is mogelijk nog niet gepubliceerd.'
                    show_paste = True
                else:
                    scraped, to_remove = _diff_startlist(riders_found)

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 403:
                    error = ('PCS blokkeert automatische verzoeken (403). '
                             'Gebruik de handmatige methode hieronder.')
                else:
                    error = f'Fout bij ophalen: {e}'
                show_paste = True
            except requests.exceptions.RequestException as e:
                error = f'Kon de pagina niet bereiken: {e}'
                show_paste = True

        elif action == 'parse_paste':
            pasted = request.form.get('paste_text', '').strip()
            if not pasted:
                error = 'Plak eerst tekst in het veld.'
                show_paste = True
            else:
                riders_found = _parse_names_from_pcs_html(pasted)
                if not riders_found:
                    error = ('Geen namen herkend. Zorg dat de tekst namen bevat '
                             'in het formaat ACHTERNAAM Voornaam, of één naam per regel.')
                    show_paste = True
                else:
                    scraped, to_remove = _diff_startlist(riders_found)

        elif action == 'import':
            names = request.form.getlist('import_names')
            teams = request.form.getlist('import_teams')
            remove_ids = [int(x) for x in request.form.getlist('remove_ids')]
            while len(teams) < len(names):
                teams.append('')

            added = updated = removed = affected_geel = affected_rood = 0

            for name, team in zip(names, teams):
                name = name.strip()
                team = team.strip() or None
                existing = Rider.query.filter_by(name=name).first()
                if existing:
                    if existing.team != team:
                        existing.team = team
                        updated += 1
                elif name:
                    db.session.add(Rider(name=name, team=team))
                    added += 1

            for rid in remove_ids:
                r = Rider.query.get(rid)
                if not r:
                    continue
                geel = Selection.query.filter_by(rider_id=rid, type='geel').count()
                rood = RoodEntry.query.filter_by(matched_rider_id=rid).count()
                affected_geel += geel
                affected_rood += rood
                # Unlink rood entries, delete geel selections, then delete rider
                RoodEntry.query.filter_by(matched_rider_id=rid).update(
                    {'matched_rider_id': None})
                Selection.query.filter_by(rider_id=rid, type='geel').delete()
                db.session.delete(r)
                removed += 1

            db.session.commit()

            parts = []
            if added:
                parts.append(f'{added} toegevoegd')
            if updated:
                parts.append(f'{updated} team bijgewerkt')
            if removed:
                parts.append(f'{removed} verwijderd')
            flash('Startlijst gesynchroniseerd: ' + ', '.join(parts) + '.', 'success')
            if affected_geel or affected_rood:
                flash(
                    f'Let op: {affected_geel} geel-selectie(s) en {affected_rood} rood-koppeling(en) '
                    f'verwijderd door het weghalen van renners. Controleer de betrokken deelnemers.',
                    'warning')
            return redirect(url_for('admin_renners'))

    return render_template('admin/scrape_startlist.html',
                           scraped=scraped, to_remove=to_remove,
                           error=error, show_paste=show_paste,
                           url=PCS_STARTLIST_URL)


@app.route('/admin/import-csv', methods=['GET', 'POST'])
@require_admin
def admin_import_csv():
    preview = None
    columns = None
    csv_data = None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'upload':
            f = request.files.get('csvfile')
            if not f or not f.filename:
                flash('Selecteer een CSV bestand.', 'danger')
                return redirect(url_for('admin_import_csv'))
            raw = f.read().decode('utf-8-sig')  # utf-8-sig handles Excel BOM
            reader = csv.DictReader(io.StringIO(raw))
            rows = list(reader)
            if not rows:
                flash('CSV is leeg.', 'danger')
                return redirect(url_for('admin_import_csv'))
            columns = list(rows[0].keys())
            preview = rows[:5]
            csv_data = raw
            return render_template('admin/import_csv.html', columns=columns,
                                   preview=preview, csv_data=csv_data)

        elif action == 'import':
            raw = request.form.get('csv_data', '')
            name_col = request.form.get('name_col')
            cluster_col = request.form.get('cluster_col') or None
            bonus_cols = request.form.getlist('bonus_cols')  # list of bonus question columns

            reader = csv.DictReader(io.StringIO(raw))
            rows = list(reader)

            # Ensure BonusQuestion records exist for each selected column
            bonus_questions = {}   # col -> BonusQuestion
            for col in bonus_cols:
                bq = BonusQuestion.query.filter_by(question=col).first()
                if not bq:
                    q_num = (db.session.query(db.func.max(BonusQuestion.number)).scalar() or 0) + 1
                    bq = BonusQuestion(number=q_num, question=col)
                    db.session.add(bq)
                    db.session.flush()
                bonus_questions[col] = bq

            added = skipped = bonus_saved = 0
            for row in rows:
                name = row.get(name_col, '').strip()
                if not name:
                    continue
                cluster_name = row.get(cluster_col, '').strip() if cluster_col else None
                cluster_id = None
                if cluster_name:
                    c = Cluster.query.filter_by(name=cluster_name).first()
                    if not c:
                        c = Cluster(name=cluster_name)
                        db.session.add(c)
                        db.session.flush()
                    cluster_id = c.id

                p = Participant.query.filter_by(name=name).first()
                if p:
                    skipped += 1
                else:
                    p = Participant(name=name, cluster_id=cluster_id)
                    db.session.add(p)
                    db.session.flush()
                    added += 1

                # Store bonus question answers (answer_text only; admin marks correct later)
                for col, bq in bonus_questions.items():
                    answer_text = row.get(col, '').strip()
                    if answer_text:
                        existing = BonusAnswer.query.filter_by(
                            question_id=bq.id, participant_id=p.id).first()
                        if not existing:
                            db.session.add(BonusAnswer(
                                question_id=bq.id,
                                participant_id=p.id,
                                correct=False,
                                answer_text=answer_text))
                            bonus_saved += 1

            db.session.commit()
            msg = f'{added} deelnemers geïmporteerd, {skipped} overgeslagen.'
            if bonus_saved:
                msg += f' {bonus_saved} bonusantwoorden opgeslagen.'
            flash(msg, 'success')
            return redirect(url_for('admin_deelnemers'))

    return render_template('admin/import_csv.html', columns=None, preview=None, csv_data=None)


@app.route('/admin/import-teams', methods=['GET', 'POST'])
@require_admin
def admin_import_teams():
    riders = Rider.query.order_by(Rider.name).all()
    rider_index = build_rider_index(riders)
    riders_by_id = {r.id: r for r in riders}

    if request.method == 'POST':
        action = request.form.get('action')

        # ── Step 1: upload CSV ────────────────────────────────────────────────
        if action == 'upload':
            f = request.files.get('csvfile')
            if not f or not f.filename:
                flash('Selecteer een CSV bestand.', 'danger')
                return redirect(url_for('admin_import_teams'))
            raw = f.read().decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(raw))
            rows = list(reader)
            if not rows:
                flash('CSV is leeg.', 'danger')
                return redirect(url_for('admin_import_teams'))
            session['teams_csv'] = raw
            columns = list(rows[0].keys())
            return render_template('admin/import_teams.html',
                                   step='map', columns=columns,
                                   preview=rows[:3], riders=riders)

        # ── Step 2: map columns → run fuzzy matching ─────────────────────────
        elif action == 'match':
            raw = session.get('teams_csv', '')
            reader = csv.DictReader(io.StringIO(raw))
            rows = list(reader)

            participant_col = request.form.get('participant_col')
            # Geel columns: all checked geel_col_* fields
            geel_cols = request.form.getlist('geel_cols')
            rood_cols = request.form.getlist('rood_cols')

            AUTO_THRESHOLD = 82  # % confidence to auto-accept
            results = []  # one entry per participant

            for row in rows:
                p_name = row.get(participant_col, '').strip()
                if not p_name:
                    continue

                def process_cols(cols, sel_type):
                    matches = []
                    for col in cols:
                        raw_name = row.get(col, '').strip()
                        if not raw_name:
                            continue
                        rider_id, score, auto = match_rider_name(
                            raw_name, rider_index, AUTO_THRESHOLD)
                        rider = riders_by_id.get(rider_id) if rider_id else None
                        matches.append({
                            'raw': raw_name,
                            'rider_id': rider_id,
                            'rider_name': rider.name if rider else None,
                            'score': score,
                            'auto': auto,
                            'col': col,
                            'type': sel_type,
                        })
                    return matches

                geel_matches = process_cols(geel_cols, 'geel')
                rood_matches = process_cols(rood_cols, 'rood')

                results.append({
                    'participant': p_name,
                    'geel': geel_matches,
                    'rood': rood_matches,
                })

            session['teams_results'] = json.dumps(results)
            needs_review = any(
                not m['auto']
                for r in results
                for m in r['geel'] + r['rood']
                if m['raw']
            )
            return render_template('admin/import_teams.html',
                                   step='review', results=results,
                                   riders=riders, needs_review=needs_review)

        # ── Step 3: save confirmed matches ────────────────────────────────────
        elif action == 'save':
            raw_results = session.get('teams_results', '[]')
            results = json.loads(raw_results)
            saved = 0

            for r in results:
                p_name = r['participant']
                p = Participant.query.filter_by(name=p_name).first()
                if not p:
                    p = Participant(name=p_name)
                    db.session.add(p)
                    db.session.flush()

                # Geel: saved as Selection (rider_id required)
                Selection.query.filter_by(participant_id=p.id, type='geel').delete()
                seen = set()
                for i, m in enumerate(r['geel']):
                    override_key = f"override_{r['participant']}_geel_{i}"
                    override_id = request.form.get(override_key)
                    rider_id = int(override_id) if override_id else m.get('rider_id')
                    if rider_id and rider_id not in seen:
                        db.session.add(Selection(
                            participant_id=p.id, rider_id=rider_id, type='geel'))
                        seen.add(rider_id)
                        saved += 1

                # Rood: saved as RoodEntry (free-text + optional match)
                RoodEntry.query.filter_by(participant_id=p.id).delete()
                for i, m in enumerate(r['rood']):
                    override_key = f"override_{r['participant']}_rood_{i}"
                    override_id = request.form.get(override_key)
                    rider_id = int(override_id) if override_id else m.get('rider_id')
                    raw_name = m.get('raw', '').strip()
                    if raw_name:
                        db.session.add(RoodEntry(
                            participant_id=p.id,
                            custom_name=raw_name,
                            matched_rider_id=rider_id if rider_id else None,
                            position=i + 1,
                        ))
                        saved += 1

            db.session.commit()
            session.pop('teams_csv', None)
            session.pop('teams_results', None)
            flash(f'{saved} renner-selecties opgeslagen.', 'success')
            return redirect(url_for('admin_teams'))

    return render_template('admin/import_teams.html', step='upload',
                           columns=None, preview=None, riders=riders)


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    debug = os.environ.get('FLASK_ENV') != 'production'
    app.run(debug=debug, port=5001)
