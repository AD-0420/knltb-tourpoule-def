from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Cluster(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    participants = db.relationship('Participant', backref='cluster', lazy=True)


class Participant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    cluster_id = db.Column(db.Integer, db.ForeignKey('cluster.id'), nullable=True)
    edit_token = db.Column(db.String(36), nullable=True, unique=True)
    selections = db.relationship('Selection', backref='participant', lazy=True, cascade='all, delete-orphan')
    rood_entries = db.relationship('RoodEntry', backref='participant', lazy=True, cascade='all, delete-orphan')
    bonus_answers = db.relationship('BonusAnswer', backref='participant', lazy=True, cascade='all, delete-orphan')


class Rider(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    team = db.Column(db.String(150), nullable=True)
    niet_gestart = db.Column(db.Boolean, default=False)
    selections = db.relationship('Selection', backref='rider', lazy=True)
    rood_entries = db.relationship('RoodEntry', foreign_keys='RoodEntry.matched_rider_id',
                                   backref='matched_rider', lazy=True)
    stage_results = db.relationship('StageResult', backref='rider', lazy=True)
    jersey_wearings = db.relationship('JerseyWearer', backref='rider', lazy=True)
    final_classifications = db.relationship('FinalClassification', backref='rider', lazy=True)


class RoodEntry(db.Model):
    """Free-text rood team entry. custom_name is what the participant typed;
    matched_rider_id is set by admin after startlist confirmation."""
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.Integer, db.ForeignKey('participant.id'), nullable=False)
    custom_name = db.Column(db.String(150), nullable=False)
    matched_rider_id = db.Column(db.Integer, db.ForeignKey('rider.id'), nullable=True)
    position = db.Column(db.Integer, nullable=True)


class Selection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.Integer, db.ForeignKey('participant.id'), nullable=False)
    rider_id = db.Column(db.Integer, db.ForeignKey('rider.id'), nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'geel'
    __table_args__ = (db.UniqueConstraint('participant_id', 'rider_id', 'type'),)


class Stage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, nullable=False, unique=True)
    results = db.relationship('StageResult', backref='stage', lazy=True,
                              cascade='all, delete-orphan', order_by='StageResult.position')
    jersey_wearers = db.relationship('JerseyWearer', backref='stage', lazy=True,
                                     cascade='all, delete-orphan')


class StageResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    stage_id = db.Column(db.Integer, db.ForeignKey('stage.id'), nullable=False)
    position = db.Column(db.Integer, nullable=False)
    rider_id = db.Column(db.Integer, db.ForeignKey('rider.id'), nullable=False)
    __table_args__ = (db.UniqueConstraint('stage_id', 'position'),)


class JerseyWearer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    stage_id = db.Column(db.Integer, db.ForeignKey('stage.id'), nullable=False)
    jersey_type = db.Column(db.String(20), nullable=False)
    rider_id = db.Column(db.Integer, db.ForeignKey('rider.id'), nullable=False)
    __table_args__ = (db.UniqueConstraint('stage_id', 'jersey_type'),)


class FinalClassification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    jersey_type = db.Column(db.String(20), nullable=False)
    position = db.Column(db.Integer, nullable=False)
    rider_id = db.Column(db.Integer, db.ForeignKey('rider.id'), nullable=False)
    __table_args__ = (db.UniqueConstraint('jersey_type', 'position'),)


class BonusQuestion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, nullable=False, unique=True)
    question = db.Column(db.Text)
    answers = db.relationship('BonusAnswer', backref='question', lazy=True,
                              cascade='all, delete-orphan')


class BonusAnswer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey('bonus_question.id'), nullable=False)
    participant_id = db.Column(db.Integer, db.ForeignKey('participant.id'), nullable=False)
    correct = db.Column(db.Boolean, default=False)
    answer_text = db.Column(db.String(255), nullable=True)
    __table_args__ = (db.UniqueConstraint('question_id', 'participant_id'),)
