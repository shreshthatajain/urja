// Node-RED settings for the public, read-only deployment.
// The editor is disabled so visitors can only view the dashboard.
module.exports = {
    flowFile: 'flows.json',
    uiPort: process.env.PORT || 1880,
    uiHost: '0.0.0.0',
    httpAdminRoot: false,                      // no editor, no admin API
    httpNodeMiddleware: function (req, res, next) {
        if (req.path === '/') { return res.redirect('/ui'); }
        next();
    },
    logging: { console: { level: 'info', metrics: false, audit: false } }
};
