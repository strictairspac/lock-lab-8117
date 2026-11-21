import {api} from './client.js';
export const migrationApi={
  state:()=>api('/receipts/migration'),
  start:batch_size=>api('/receipts/migration/start','POST',{batch_size}),
  batch:batch_size=>api('/receipts/migration/batch','POST',{batch_size}),
  run:batch_size=>api('/receipts/migration/run','POST',{batch_size}),
  switch:()=>api('/receipts/migration/switch','POST',{}),
  revert:()=>api('/receipts/migration/return','POST',{})
};
