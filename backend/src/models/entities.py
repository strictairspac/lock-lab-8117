from sqlalchemy import Column, Integer, BigInteger, String, JSON, DateTime, ForeignKey, Text, UniqueConstraint, Index
from datetime import datetime, timezone
from src.config.database import Base
class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True);email=Column(String(120),unique=True,nullable=False)
    password=Column(String(300),nullable=False);role=Column(String(20),nullable=False)
class Sample(Base):
    __tablename__='samples'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False)
    title=Column(String(160),nullable=False);quantity=Column(Integer,nullable=False);location=Column(String(120),nullable=False)
class Transfer(Base):
    __tablename__='transfers'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False)
    sample_id=Column(Integer,ForeignKey('samples.id'),nullable=False);destination=Column(String(120),nullable=False)
    quantity=Column(Integer,nullable=False);note=Column(String(300),default='')
class Trial(Base):
    __tablename__='trials'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False)
    transfer_id=Column(Integer,ForeignKey('transfers.id'),nullable=False);timeout_ms=Column(Integer,nullable=False)
    status=Column(String(20),default='queued',nullable=False);result=Column(JSON,nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc));error=Column(Text,default='')
    period=Column(String(7),nullable=True)
class TrialBalance(Base):
    __tablename__='trial_balances'
    trial_id=Column(Integer,ForeignKey('trials.id'),primary_key=True);remaining=Column(Integer,nullable=False);received=Column(Integer,nullable=False,default=0)
class Receipt(Base):
    __tablename__='receipts'
    id=Column(Integer,primary_key=True);trial_id=Column(Integer,ForeignKey('trials.id'),nullable=False)
    actor=Column(String(20),nullable=False);quantity=Column(Integer,nullable=False)
    __table_args__=(UniqueConstraint('trial_id','actor'),)
class ReceiptLedger(Base):
    """Monthly partition of receipt history. `period` is the month the trial was started,
    so a receipt committed after a month boundary still lands in its original month."""
    __tablename__='receipt_ledger'
    id=Column(BigInteger,primary_key=True,autoincrement=True)
    period=Column(String(7),primary_key=True)
    trial_id=Column(Integer,nullable=False)
    actor=Column(String(20),nullable=False)
    quantity=Column(Integer,nullable=False)
    origin=Column(String(12),nullable=False,default='live')
    source_receipt_id=Column(Integer,nullable=True)
    recorded_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),nullable=False)
    __table_args__=(
        UniqueConstraint('period','trial_id','actor',name='uq_receipt_ledger_trial_actor'),
        UniqueConstraint('period','source_receipt_id',name='uq_receipt_ledger_source'),
        Index('ix_receipt_ledger_trial','trial_id'),
        {'postgresql_partition_by':'LIST (period)'},
    )
class ReceiptMigration(Base):
    __tablename__='receipt_migrations'
    id=Column(Integer,primary_key=True)
    status=Column(String(20),nullable=False,default='idle')
    active_ledger=Column(String(20),nullable=False,default='receipts')
    cursor=Column(Integer,nullable=False,default=0)
    batch_size=Column(Integer,nullable=False,default=200)
    batches=Column(Integer,nullable=False,default=0)
    error=Column(Text,default='')
    started_at=Column(DateTime(timezone=True),nullable=True)
    updated_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),nullable=False)
    activated_at=Column(DateTime(timezone=True),nullable=True)
    returned_at=Column(DateTime(timezone=True),nullable=True)
class Audit(Base):
    __tablename__='audit_logs'
    id=Column(Integer,primary_key=True);actor=Column(String(120),nullable=False);action=Column(String(160),nullable=False)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
