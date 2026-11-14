<?php
/**
 * Test fixture only (must-use plugin on the disposable source sites).
 * The sources have no internet access, so the plugin directory lookup for
 * the "reprint" slug is answered from a zip served by the same container.
 * Everything else (REST install, wp-admin installer, activation) is stock
 * WordPress.
 */
add_filter('plugins_api', function ($result, $action, $args) {
    if (!in_array($action, ['plugin_information', 'query_plugins'], true)) {
        return $result;
    }
    $info = (object) [
        'name' => 'Reprint',
        'slug' => 'reprint',
        'version' => '0.11.5-dev',
        'author' => 'WordPress Contributors',
        'requires' => '4.7',
        'requires_php' => '5.6.20',
        'download_link' => 'http://127.0.0.1/reprint-test-download.zip',
        'short_description' => 'Reprint server',
        'sections' => ['description' => 'Reprint server'],
        'icons' => ['default' => ''],
        'language_packs' => [],
        'versions' => [],
        'compatibility' => [],
        'short_description' => 'Reprint server',
        'downloaded' => 1,
        'author_profile' => '',
        'contributors' => [],
        'rating' => 100, 'num_ratings' => 1, 'active_installs' => 1,
        'last_updated' => '2026-11-10', 'homepage' => '', 'tested' => '7.1',
    ];
    if ($action === 'query_plugins') {
        return (object) ['info' => ['page' => 1, 'pages' => 1, 'results' => 1], 'plugins' => [(array) $info]];
    }
    return $info;
}, 10, 3);
add_filter('http_request_host_is_external', '__return_true');
add_filter('http_request_args', function ($args) { $args['reject_unsafe_urls'] = false; return $args; });
