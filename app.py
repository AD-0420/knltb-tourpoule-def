import csv
import io
import json
import os
import unicodedata
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, abort, session
from functools import wraps
from rapidfuzz import fuzz, process as fuzz_process
from models import (db, Cluster, Participant, Rider, RoodEntry, Selection, Stage,
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
    # Migrate: add niet_gestart column to existing rider tables
    with db.engine.connect() as _conn:
        try:
            _conn.execute(db.text("ALTER TABLE rider ADD COLUMN niet_gestart BOOLEAN DEFAULT 0"))
            _conn.commit()
        except Exception:
            pass

POINTS_TABLE = {1: 35, 2: 25, 3: 20, 4: 18, 5: 16, 6: 14, 7: 12,
                8: 10, 9: 8, 10: 6, 11: 5, 12: 4, 13: 3, 14: 2, 15: 1}
BONUS_POINTS = 15
JERSEY_DAILY_POINTS = 1
JERSEY_LABELS = {'yellow': 'Gele trui', 'green': 'Groene trui',
                 'polka': 'Bolletjestrui', 'white': 'Witte trui'}
JERSEY_CLASSES = {'yellow': 'jersey-yellow', 'green': 'jersey-green',
                  'polka': 'jersey-polka', 'white': 'jersey-white'}
MAX_GEEL = 15
MAX_ROOD = 15
INSCHRIJF_DEADLINE = datetime(2026, 7, 3, 17, 0, 0)
INSCHRIJFGELD = '€5,-'


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.password != app.config['ADMIN_PASSWORD']:
            return ('Toegang geweigerd. Voer het admin wachtwoord in.', 401,
                    {'WWW-Authenticate': 'Basic realm="Admin"'})
        return f(*args, **kwargs)
    return decorated


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


def get_participant_scores():
    """Return list of dicts with geel/rood scores per participant."""
    rider_points = get_rider_points_map()
    bonus_counts = {}
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
    geel = sorted(scores, key=lambda x: x['geel'], reverse=True)[:5]
    rood = sorted((s for s in scores if s['has_rood']), key=lambda x: x['rood'])[:5]
    stages_done = Stage.query.count()
    return render_template('index.html', geel=geel, rood=rood, stages_done=stages_done)


@app.route('/geel')
def geel_klassement():
    scores = get_participant_scores()
    standings = sorted(scores, key=lambda x: x['geel'], reverse=True)
    for i, s in enumerate(standings):
        s['rank'] = i + 1

    clusters = Cluster.query.order_by(Cluster.name).all()
    cluster_data = {}
    for c in clusters:
        members = [s for s in standings if s['participant'].cluster_id == c.id]
        if members:
            cluster_data[c.name] = members

    unclustered = [s for s in standings if s['participant'].cluster_id is None]

    return render_template('geel.html', standings=standings,
                           cluster_data=cluster_data, unclustered=unclustered)


@app.route('/rood')
def rood_klassement():
    scores = get_participant_scores()
    standings = sorted((s for s in scores if s['has_rood']), key=lambda x: x['rood'])
    for i, s in enumerate(standings):
        s['rank'] = i + 1
    return render_template('rood.html', standings=standings)


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
    bonus_correct = BonusAnswer.query.filter_by(participant_id=pid, correct=True).count()
    geel_total += bonus_correct * BONUS_POINTS

    rood_total = sum(x['points'] for x in rood_team) if rood_team else None

    questions = BonusQuestion.query.order_by(BonusQuestion.number).all()
    answers = {ba.question_id: ba.correct
               for ba in BonusAnswer.query.filter_by(participant_id=pid).all()}

    all_participants = Participant.query.order_by(Participant.name).all()
    return render_template('deelnemer.html', p=p, geel_team=geel_team,
                           rood_team=rood_team, geel_total=geel_total,
                           rood_total=rood_total, questions=questions,
                           answers=answers, participants=all_participants)


@app.route('/etappes')
def etappes():
    stages = Stage.query.order_by(Stage.number).all()
    stage_data = []
    for stage in stages:
        jerseys = {jw.jersey_type: jw.rider for jw in stage.jersey_wearers}
        stage_data.append({'stage': stage, 'results': stage.results, 'jerseys': jerseys})
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
    return render_template('renners.html', rider_data=data, n_participants=n_participants)


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

    # Bonus points per participant (all-time, added to last stage)
    bonus_counts = {}
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
    now = datetime.now()
    gesloten = now > INSCHRIJF_DEADLINE

    if request.method == 'POST':
        if gesloten:
            flash('De inschrijving is gesloten.', 'danger')
            return redirect(url_for('inschrijven'))

        naam = request.form.get('naam', '').strip()
        afdeling = request.form.get('afdeling', '').strip()
        geel_ids = [int(x) for x in request.form.getlist('geel_riders')]
        rood_names = [request.form.get(f'rood_name_{i}', '').strip()
                      for i in range(1, 16)]
        rood_names = [n for n in rood_names if n]

        # Validatie
        errors = []
        if not naam:
            errors.append('Voer je naam in.')
        if not afdeling:
            errors.append('Voer je afdeling in.')
        if len(geel_ids) != 15:
            errors.append(f'Kies precies 15 renners voor je geel team (nu {len(geel_ids)}).')
        if rood_names and len(rood_names) != 15:
            errors.append(f'Vul precies 15 renners in voor je rood team of laat alles leeg (nu {len(rood_names)}).')

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

        p = Participant(name=naam, cluster_id=cluster_id)
        db.session.add(p)
        db.session.flush()

        for rid in geel_ids:
            db.session.add(Selection(participant_id=p.id, rider_id=rid, type='geel'))

        # Rood: free text, try to auto-match to startlist
        if rood_names:
            all_riders = Rider.query.order_by(Rider.name).all()
            r_index = build_rider_index(all_riders)
            for pos, raw_name in enumerate(rood_names, 1):
                rid, score, auto = match_rider_name(raw_name, r_index)
                matched_id = rid if auto else None
                db.session.add(RoodEntry(
                    participant_id=p.id,
                    custom_name=raw_name,
                    matched_rider_id=matched_id,
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

    return render_template('inschrijven.html', riders=riders, questions=questions,
                           gesloten=gesloten, deadline=INSCHRIJF_DEADLINE,
                           inschrijfgeld=INSCHRIJFGELD, now=now,
                           max_geel=MAX_GEEL, max_rood=MAX_ROOD)


@app.route('/inschrijven/bevestiging')
def inschrijven_bevestiging():
    naam = request.args.get('naam', 'Deelnemer')
    p = Participant.query.filter_by(name=naam).first()
    return render_template('inschrijven_bevestiging.html', naam=naam, p=p)


# ── Admin routes ───────────────────────────────────────────────────────────────

@app.route('/admin/handleiding')
@require_admin
def admin_handleiding():
    return render_template('admin/handleiding.html',
                           points_table=POINTS_TABLE,
                           bonus_points=BONUS_POINTS,
                           max_geel=MAX_GEEL,
                           max_rood=MAX_ROOD,
                           deadline=INSCHRIJF_DEADLINE)


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
            if name and not Rider.query.filter_by(name=name).first():
                db.session.add(Rider(name=name))
                db.session.commit()
                flash(f'{name} toegevoegd.', 'success')
        elif action == 'bulk_add':
            names = request.form.get('names', '')
            added = 0
            for name in names.strip().splitlines():
                name = name.strip()
                if name and not Rider.query.filter_by(name=name).first():
                    db.session.add(Rider(name=name))
                    added += 1
            db.session.commit()
            flash(f'{added} renners toegevoegd.', 'success')
        elif action == 'delete':
            rid = request.form.get('rider_id')
            r = Rider.query.get(rid)
            if r:
                db.session.delete(r)
                db.session.commit()
                flash(f'{r.name} verwijderd.', 'warning')
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
