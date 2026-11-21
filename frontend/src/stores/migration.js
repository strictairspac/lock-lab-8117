import {migrationApi} from '../api/migration.js';
export const migrationStore={items:null,async refresh(){this.items=await migrationApi.state();return this.items;}};
