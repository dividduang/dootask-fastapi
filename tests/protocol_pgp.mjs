// Real original OpenPGP.js client algorithms and framing; no PGP bypass or backend imports.
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import path from 'node:path';
import {randomBytes, createHash} from 'node:crypto';
import assert from 'node:assert/strict';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(root, '.runtime/frontend-build/package.json'));
const modulePath = require.resolve('openpgp_hi/lightweight');
const openpgp = await import(pathToFileURL(modulePath).href);
const base = process.argv[2] || 'http://127.0.0.1:18080';
const run = new Date().toISOString().replace(/[:.]/g, '-');
const evidence = {run, command: process.argv, node:process.version, crypto_module:modulePath, crypto_sha256:createHash('sha256').update(await readFile(modulePath)).digest('hex'), frames:[], status:'FAIL'};
const secrets = [];
const hash = value => createHash('sha256').update(value).digest('hex');
const redact = value => {
  let text = JSON.stringify(value);
  for (const secret of secrets) text = text.split(secret).join('[REDACTED]');
  return JSON.parse(text);
};
const strip = (text, kind) => text.replace(new RegExp('\\s*-----(BEGIN|END) PGP '+kind+'-----\\s*', 'g'), '').trim();
const clientId = 'protocol_' + randomBytes(12).toString('hex');
const password = 'Protocol-' + randomBytes(12).toString('base64url') + '9!';
secrets.push(password,clientId);
try {
  const keys = await openpgp.generateKey({type:'ecc',curve:'curve25519',passphrase:clientId,userIDs:[{name:'doo',email:'admin@admin.com'}]});
  const packed = strip(keys.publicKey,'PUBLIC KEY BLOCK').replace(/\+/g,'-').replace(/\//g,'_').replace(/\n/g,'$');
  const privateKey = await openpgp.decryptKey({privateKey:await openpgp.readPrivateKey({armoredKey:keys.privateKey}),passphrase:clientId});
  const exchangeResponse = await fetch(base+'/api/users/key/client',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:clientId})});
  const exchange = await exchangeResponse.json();
  assert.equal(exchange.ret,1);
  assert.equal(exchange.data.type,'pgp');
  evidence.exchange = {status:exchangeResponse.status,ret:exchange.ret,public_key_sha256:hash(exchange.data.key)};
  const serverKey = await openpgp.readKey({armoredKey:exchange.data.key});
  const email='pgp-'+randomBytes(7).toString('hex')+'@example.test';
  async function encryptedCall(name, input, token='', expectSuccess=true) {
    const encrypted=strip(await openpgp.encrypt({message:await openpgp.createMessage({text:JSON.stringify(input)}),encryptionKeys:serverKey}),'MESSAGE');
    const start=Date.now();
    const response=await fetch(base+'/api/'+name,{method:'POST',headers:{'Content-Type':'application/json',encrypt:`encrypt_type=pgp;encrypt_id=${exchange.data.id};client_type=pgp;client_key=${packed}`,...(token?{'dootask-token':token}:{})},body:JSON.stringify({encrypted})});
    const wire=await response.text();
    const wrapper=JSON.parse(wire);
    assert.equal(typeof wrapper.encrypted,'string','Valid encrypted request must receive encrypted response');
    const decrypted=await openpgp.decrypt({message:await openpgp.readMessage({armoredMessage:'-----BEGIN PGP MESSAGE-----\n\n'+wrapper.encrypted+'\n-----END PGP MESSAGE-----'}),decryptionKeys:privateKey});
    const result=JSON.parse(decrypted.data);
    if(result.data?.token) secrets.push(result.data.token);
    if(result.data?.encrypt) secrets.push(result.data.encrypt);
    evidence.frames.push({utc:new Date().toISOString(),path:name,http:response.status,elapsed_ms:Date.now()-start,request_ciphertext_sha256:hash(encrypted),request_ciphertext_bytes:Buffer.byteLength(encrypted),response_ciphertext_sha256:hash(wire),response_ciphertext_bytes:Buffer.byteLength(wire),decrypted:redact(result)});
    assert.equal(response.status,200);
    assert.equal(result.ret===1,expectSuccess,`${name}: ${result.msg}`);
    return result;
  }
  const registration=await encryptedCall('users/login',{type:'reg',email,password});
  assert.ok(registration.data.userid && registration.data.token);
  await encryptedCall('users/logout',{},registration.data.token);
  const login=await encryptedCall('users/login',{email,password});
  assert.equal(login.data.userid,registration.data.userid);
  await encryptedCall('users/info',{},login.data.token);
  await encryptedCall('users/login',{email,password:password+'wrong'},'',false);
  await encryptedCall('users/logout',{},login.data.token);
  await encryptedCall('users/info',{},login.data.token,false);
  evidence.status='PASS';
  evidence.contract='Original openpgp_hi/lightweight 5.7.0-1 Curve25519 client, RSA3072 server exchange, encrypted registration/login/response/errors/logout/revocation';
} catch(error) {
  evidence.error=error.stack;
  process.exitCode=1;
} finally {
  const out=path.join(root,'docs/evidence/qa-protocol');
  await mkdir(out,{recursive:true});
  const destination=path.join(out,run+'-pgp.json');
  await writeFile(destination,JSON.stringify(redact(evidence),null,2),'utf8');
  console.log(JSON.stringify({evidence:destination,status:evidence.status,error:evidence.error}));
}
