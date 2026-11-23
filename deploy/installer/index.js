import installer from '../../install.sh';

export default {
  fetch(request) {
    const { pathname } = new URL(request.url);
    if (pathname !== '/' && pathname !== '/install.sh') {
      return new Response('Not found\n', { status: 404 });
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method not allowed\n', {
        status: 405,
        headers: { Allow: 'GET, HEAD' },
      });
    }
    return new Response(request.method === 'HEAD' ? null : installer, {
      headers: {
        'Content-Type': 'text/plain; charset=utf-8',
        'Cache-Control': 'public, max-age=300',
        'X-Content-Type-Options': 'nosniff',
      },
    });
  },
};
