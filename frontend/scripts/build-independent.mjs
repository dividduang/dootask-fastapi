import {build} from 'vite';

// Use the existing production entry/config without the destructive legacy cmd wrapper.
process.argv.push('fromcmd');
await build();
