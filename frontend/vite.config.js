import fs from "fs";
import path from "path";
import {defineConfig, loadEnv} from 'vite'
import {createVuePlugin} from 'vite-plugin-vue2';
import vitePluginRequire from 'vite-plugin-require'
import vitePluginFileCopy from 'vite-plugin-file-copy';
import autoprefixer from 'autoprefixer';
import chokidar from 'chokidar';

const argv = process.argv;
const basePath = argv.includes('electronBuild') ? './' : '/';
const publicPath = argv.includes('electronBuild') ? 'electron/public' : 'public';
const staticDir = {src: path.resolve(__dirname, 'resources/assets/statics/public'), dest: path.resolve(__dirname, publicPath)}

export default defineConfig(({command, mode}) => {
    const env = loadEnv(mode, process.cwd(), '')
    const host = "127.0.0.1"
    const port = parseInt(env['APP_DEV_PORT'] || "5173")
    const proxy_uri = env['VSCODE_PROXY_URI']

    const plugins = [
        createVuePlugin({
            template: {
                compilerOptions: {
                    isCustomElement: (tag) => tag.includes('micro-app'),
                }
            }
        }),
        vitePluginRequire(),
        vitePluginFileCopy([staticDir])
    ]
    if (mode === "development") {
        plugins.push({
            name: 'watch-copy',
            configureServer() {
                chokidar.watch(staticDir.src, {
                    ignoreInitial: true,
                }).on('all', (event, filePath) => {
                    if (['add', 'change', 'unlink'].includes(event)) {
                        const relativePath = path.relative(staticDir.src, filePath);
                        const destPath = path.resolve(staticDir.dest, relativePath);
                        if (event === 'unlink') {
                            if (fs.existsSync(destPath)) {
                                fs.unlinkSync(destPath);
                                console.log(`Removed ${destPath}`);
                            }
                            return;
                        }
                        if (path.basename(filePath) === '.DS_Store') {
                            return;
                        }
                        fs.mkdirSync(path.dirname(destPath), {recursive: true});
                        fs.copyFileSync(filePath, destPath);
                        console.log(`Copied ${filePath} to ${destPath}`);
                    }
                });
            }
        })
    }

    const serverHmr = {}
    if (/^https?:\/\//i.test(proxy_uri)) {
        const proxyUri = new URL(proxy_uri)
        if (proxyUri) {
            Object.assign(serverHmr, {
                host: proxyUri.host,
                clientPort: proxyUri.port || (/^https/.test(proxy_uri) ? 443 : 80)
            })
        }
    }

    return {
        base: basePath,
        publicDir: publicPath,
        server: {
            host,
            port,
            strictPort: false,
            watch: {
                ignored: [
                    '**/node_modules/**',
                    '**/storage/**',
                    '**/public/uploads/**',
                    '**/database/**',
                    '**/docker/**',
                    '**/tests/**',
                    '**/language/**',
                    '**/electron/**',
                ]
            },
            hmr: serverHmr
        },
        resolve: {
            alias: {
                '~element-sea': path.resolve(__dirname, 'node_modules/element-sea'),
                '~quill-hi': path.resolve(__dirname, 'node_modules/quill-hi'),
                '~quill-mention-hi': path.resolve(__dirname, 'node_modules/quill-mention-hi'),
                '../images': path.resolve(__dirname, command === 'serve' ? '/images' : 'resources/assets/statics/public/images'),
                '../css': path.resolve(__dirname, command === 'serve' ? '/css' : 'resources/assets/statics/public/css')
            },
            extensions: ['.mjs', '.js', '.ts', '.jsx', '.tsx', '.json', '.vue']
        },
        build: {
            manifest: true,
            outDir: publicPath,
            assetsDir: "js/build",
            emptyOutDir: false,
            rollupOptions: {
                input: 'resources/assets/js/app.js',
                output: {
                    manualChunks(id) {
                        if (id.includes('node_modules')) {
                            return id.toString().split('node_modules/')[1].split('/')[0].toString();
                        }
                    }
                }
            },
            brotliSize: false,
            chunkSizeWarningLimit: 1500,
        },
        plugins,
        css: {
            postcss: {
                plugins: [
                    autoprefixer
                ]
            }
        }
    };
});
