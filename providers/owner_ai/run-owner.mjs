import {executeJob} from './officialCli.mjs'
let input='';for await(const chunk of process.stdin){input+=chunk;if(Buffer.byteLength(input)>200000)throw new Error('Input exceeds limit')}
try{const job=JSON.parse(input);const result=await executeJob(job,{env:process.env});if(!result){console.log(JSON.stringify({ok:false}));process.exitCode=1}else console.log(JSON.stringify(result))}catch{console.log(JSON.stringify({ok:false}));process.exitCode=1}
