from fastapi import APIRouter,Depends
from src.config.database import db_session
from src.utils.security import current,writer,admin
from src.schemas.trial import TrialInput,ReceiptMigrationInput
from src.models.entities import Audit
from src.services import trials as service
from src.services import receipts
router=APIRouter()

def _audit(db,user,action,detail):
    db.add(Audit(actor=user.email,action=f'{action}：{detail}'))

@router.get('/trials')
def listing(db=Depends(db_session),user=Depends(current)): return service.listing(db)
@router.post('/trials',status_code=201)
def create(data:TrialInput,db=Depends(db_session),user=Depends(writer)): return service.create(db,data,user)
from fastapi.responses import Response
import json
@router.get('/trials/{ident}')
def detail(ident:int,db=Depends(db_session),user=Depends(current)): return service.detail(db,ident)
@router.get('/trials/{ident}/export')
def export(ident:int,db=Depends(db_session),user=Depends(current)):
    obj=service.detail(db,ident)
    return Response(json.dumps(obj,ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="trial-{ident}.json"'})

@router.get('/receipts/migration')
def migration(db=Depends(db_session),user=Depends(current)): return receipts.summary(db)

@router.post('/receipts/migration/start')
def migration_start(data:ReceiptMigrationInput|None=None,db=Depends(db_session),user=Depends(admin)):
    size=data.batch_size if data else None
    result=receipts.start(db,size)
    _audit(db,user,'发起回执迁移',f"批次大小 {result['batch_size']}，状态 {result['status']}")
    db.commit();return result

@router.post('/receipts/migration/batch')
def migration_batch(data:ReceiptMigrationInput|None=None,db=Depends(db_session),user=Depends(admin)):
    result=receipts.run_batch(db,data.batch_size if data else None)
    db.commit();return result

@router.post('/receipts/migration/run')
def migration_run(data:ReceiptMigrationInput|None=None,db=Depends(db_session),user=Depends(admin)):
    result=receipts.run_all(db,data.batch_size if data else None)
    _audit(db,user,'回填回执至核齐',f"{result['batches_run']} 批，游标 {result['cursor']}")
    db.commit();return result

@router.post('/receipts/migration/switch')
def migration_switch(db=Depends(db_session),user=Depends(admin)):
    result=receipts.switch(db)
    _audit(db,user,'切换回执账本',f"启用 {result['active_ledger']}，补入 {result['reconciled_rows']} 行")
    db.commit();return result

@router.post('/receipts/migration/return')
def migration_return(db=Depends(db_session),user=Depends(admin)):
    result=receipts.return_to_legacy(db)
    _audit(db,user,'退回旧回执表',f"恢复 {result['active_ledger']}")
    db.commit();return result
