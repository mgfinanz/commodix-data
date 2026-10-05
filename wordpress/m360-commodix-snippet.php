/**
 * M360 CommodiX – Datenablage, REST-API und Startseiten-Kachel
 * Daten: Option m360cx_data (JSON {order, assets}), Zusammenfassung: m360cx_summary
 * GET  /wp-json/m360/v1/commodix          -> komplette Signaldaten (öffentlich)
 * GET  /wp-json/m360/v1/commodix/summary  -> Kurzfassung für die Kachel (öffentlich)
 * POST /wp-json/m360/v1/commodix  {data}   -> speichern (nur Administratoren)
 *      Neue Kauf-/Verkaufssignale (Wechsel ggü. dem letzten Stand) -> Telegram-Kanal über m360tg_send()
 *      aus dem Snippet „M360 Telegram-Kanal“. Body-Option notify:false unterdrückt die Meldung.
 * POST /wp-json/m360/v1/commodix/telegram-test -> aktuelle Kauf-/Verkaufssignale einmalig senden (Admin)
 * POST /wp-json/m360/v1/commodix/telegram-post {html,key} -> freie Meldung, z. B. Vorbörse 10 Uhr (Admin, key = einmalig)
 */
if ( ! defined( 'ABSPATH' ) ) { exit; }

if ( ! function_exists( 'm360cx_summary_from' ) ) {
	function m360cx_summary_from( $data ) {
		$out = array( 'asof' => '', 'counts' => array( 'buy' => 0, 'sell' => 0, 'neutral' => 0 ), 'assets' => array() );
		if ( empty( $data['assets'] ) || ! is_array( $data['assets'] ) ) { return $out; }
		$order = ! empty( $data['order'] ) ? $data['order'] : array_keys( $data['assets'] );
		foreach ( $order as $t ) {
			if ( empty( $data['assets'][ $t ] ) ) { continue; }
			$a   = $data['assets'][ $t ];
			$sig = isset( $a['signal'] ) ? (string) $a['signal'] : '';
			if ( strpos( $sig, 'KAUF' ) === 0 ) { $out['counts']['buy']++; }
			elseif ( strpos( $sig, 'VERK' ) === 0 ) { $out['counts']['sell']++; }
			else { $out['counts']['neutral']++; }
			if ( ! $out['asof'] && ! empty( $a['asof'] ) ) { $out['asof'] = (string) $a['asof']; }
			$out['assets'][] = array(
				't'     => $t,
				'sig'   => $sig,
				'score' => isset( $a['score_pct'] ) ? (float) $a['score_pct'] : 0,
				'last'  => isset( $a['levels']['last'] ) ? (float) $a['levels']['last'] : 0,
			);
		}
		return $out;
	}
}

if ( ! function_exists( 'm360cx_tg_message' ) ) {
	/** Nachricht für neue Signale. $items = Liste von Tickern. */
	function m360cx_tg_message( $data, $items, $prev, $headline ) {
		$e   = function_exists( 'm360tg_esc' ) ? 'm360tg_esc' : 'esc_html';
		$num = function ( $v, $d = 2 ) { return number_format( (float) $v, $d, ',', '.' ); };
		$asof = '';
		$blocks = array();
		foreach ( $items as $t ) {
			$a = $data['assets'][ $t ];
			if ( ! $asof && ! empty( $a['asof'] ) ) { $asof = date_i18n( 'd.m.Y', strtotime( $a['asof'] . ' 12:00:00' ) ); }
			$sig  = (string) $a['signal'];
			$buy  = strpos( $sig, 'KAUF' ) === 0;
			$icon = $buy ? '🟢' : '🔴';
			$name = isset( $a['name'] ) ? $a['name'] : $t;
			$b    = $icon . ' <b>' . $e( $sig ) . '</b> – <b>' . $e( $t ) . '</b> (' . $e( $name ) . ')';
			if ( ! empty( $prev[ $t ] ) && $prev[ $t ] !== $sig ) { $b .= "\n<i>vorher: " . $e( $prev[ $t ] ) . '</i>'; }
			$sc   = isset( $a['score_pct'] ) ? (float) $a['score_pct'] : 0;
			$b   .= "\nKurs " . $num( $a['levels']['last'] ) . ' $ · Score ' . ( $sc > 0 ? '+' : '' ) . $num( $sc, 0 ) . ' %';
			$above = (float) $a['levels']['last'] > (float) $a['levels']['sma200'];
			$plan  = isset( $a['backtest']['modes']['both']['option_plan'] ) ? $a['backtest']['modes']['both']['option_plan'] : array();
			if ( ! empty( $plan['dir'] ) ) {
				$lim = (float) $plan['limit'];
				$tp  = isset( $a['backtest']['modes']['both']['options']['settings']['take_profit_pct'] ) ? (float) $a['backtest']['modes']['both']['options']['settings']['take_profit_pct'] : 20;
				$sl  = isset( $a['backtest']['modes']['both']['options']['settings']['stop_loss_pct'] ) ? (float) $a['backtest']['modes']['both']['options']['settings']['stop_loss_pct'] : 40;
				$b  .= "\nOption: " . $e( $plan['action'] ) . ' · Limit ' . $num( $lim ) . ' $';
				if ( $tp ) { $b .= ' · Gewinnmitnahme ' . $num( $lim * ( 1 + $tp / 100 ) ); }
				if ( $sl ) { $b .= ' · Stop-Loss ' . $num( $lim * ( 1 - $sl / 100 ) ); }
				if ( ! empty( $plan['modelled'] ) ) { $b .= ' <i>(modelliert, Kette live prüfen)</i>'; }
				$b .= "\nAusstieg Basiswert: " . $num( $plan['underlying_stop'] ) . ' $';
			} else {
				$b .= "\n<i>Keine neue Position: SMA-200-Filter (" . ( $above ? 'Kurs über SMA 200, Short gesperrt' : 'Kurs unter SMA 200, Long gesperrt' ) . ').</i>';
			}
			$blocks[] = $b;
		}
		$html  = '📊 <b>' . $e( $headline ) . '</b>' . ( $asof ? ' · Schluss ' . $asof : '' ) . "\n\n";
		$html .= implode( "\n\n", $blocks );
		$html .= "\n\n<i>Regelbasiertes Signal, keine Anlageberatung. Optionen können wertlos verfallen.</i>";
		return $html;
	}
	function m360cx_tg_send( $html ) {
		if ( ! function_exists( 'm360tg_send' ) ) { return new WP_Error( 'm360cx_tg', 'Snippet „M360 Telegram-Kanal“ nicht aktiv' ); }
		if ( function_exists( 'm360tg_ready' ) && ! m360tg_ready() ) { return new WP_Error( 'm360cx_tg', 'Telegram-Bot nicht eingerichtet' ); }
		$url = home_url( '/commodix/' );
		if ( function_exists( 'm360tg_link' ) ) { $url = m360tg_link( $url, 'commodix' ); }
		$res = m360tg_send( $html, '', 'Alle Signale ansehen', $url );
		if ( function_exists( 'm360tg_log' ) ) {
			m360tg_log( 'CommodiX-Signal ' . ( is_wp_error( $res ) ? 'Fehler: ' . $res->get_error_message() : 'gesendet' ), ! is_wp_error( $res ) );
		}
		return $res;
	}
}

if ( ! function_exists( 'm360cx_store' ) ) {
	/** Daten speichern, Zusammenfassung bilden, neue Kauf-/Verkaufssignale an Telegram melden. */
	function m360cx_store( $data, $notify = true ) {
		$old  = get_option( 'm360cx_summary', array() );
		$prev = array();
		if ( ! empty( $old['assets'] ) ) { foreach ( $old['assets'] as $x ) { $prev[ $x['t'] ] = $x['sig']; } }
		// Vergleichsbasis für Telegram = letzter Tagesschluss (Zwischenstände zählen nicht)
		$final = empty( $data['status'] ) || ! empty( $data['status']['final'] );
		$fin   = get_option( 'm360cx_final_sigs', false );
		if ( false === $fin ) { $fin = $prev; update_option( 'm360cx_final_sigs', $fin, false ); }
		$prev = is_array( $fin ) ? $fin : $prev;
		$json = wp_json_encode( $data, JSON_UNESCAPED_UNICODE );
		update_option( 'm360cx_data', $json, false );
		$sum = m360cx_summary_from( $data );
		$sum['updated'] = current_time( 'mysql' );
		$sum['status']  = isset( $data['status'] ) ? $data['status'] : array( 'date' => $sum['asof'], 'time' => '', 'final' => true );
		update_option( 'm360cx_summary', $sum, false );
		if ( ! $final ) {
			return array( 'ok' => true, 'bytes' => strlen( $json ), 'assets' => count( $data['assets'] ), 'asof' => $sum['asof'], 'counts' => $sum['counts'], 'telegram' => 'Zwischenstand – keine Telegram-Meldung' );
		}
		$nowfin = array();
		foreach ( $sum['assets'] as $x ) { $nowfin[ $x['t'] ] = $x['sig']; }
		update_option( 'm360cx_final_sigs', $nowfin, false );
		// Neue Kauf-/Verkaufssignale = Signal ist KAUF/VERK und war vorher anders (nur wenn es einen Vorstand gibt)
		$new = array();
		if ( $prev ) {
			foreach ( $sum['assets'] as $x ) {
				$is = strpos( $x['sig'], 'KAUF' ) === 0 || strpos( $x['sig'], 'VERK' ) === 0;
				if ( $is && ( ! isset( $prev[ $x['t'] ] ) || $prev[ $x['t'] ] !== $x['sig'] ) ) { $new[] = $x['t']; }
			}
		}
		$tg = 'keine neuen Signale';
		if ( $new ) {
			$key  = $sum['asof'] . '|' . implode( ',', $new );
			$sent = get_option( 'm360cx_tg_sent', '' );
			if ( ! $notify ) {
				$tg = 'unterdrückt: ' . implode( ', ', $new );
			} elseif ( $sent === $key ) {
				$tg = 'bereits gesendet: ' . implode( ', ', $new );
			} else {
				$res = m360cx_tg_send( m360cx_tg_message( $data, $new, $prev, count( $new ) > 1 ? 'Neue CommodiX-Signale' : 'Neues CommodiX-Signal' ) );
				if ( is_wp_error( $res ) ) { $tg = 'Fehler: ' . $res->get_error_message(); }
				else { update_option( 'm360cx_tg_sent', $key, false ); $tg = 'gesendet: ' . implode( ', ', $new ); }
			}
		}
		return array( 'ok' => true, 'bytes' => strlen( $json ), 'assets' => count( $data['assets'] ), 'asof' => $sum['asof'], 'counts' => $sum['counts'], 'telegram' => $tg );
	}
}

add_action( 'rest_api_init', function () {
	register_rest_route( 'm360/v1', '/commodix', array(
		array(
			'methods'             => 'GET',
			'permission_callback' => '__return_true',
			'callback'            => function () {
				$raw = get_option( 'm360cx_data', '' );
				if ( ! $raw ) { return new WP_Error( 'm360cx_empty', 'Noch keine Daten', array( 'status' => 404 ) ); }
				$r = new WP_REST_Response( json_decode( $raw, true ) );
				$r->header( 'Cache-Control', 'no-cache, must-revalidate, max-age=0' );
				return $r;
			},
		),
		array(
			'methods'             => 'POST',
			'permission_callback' => function () { return current_user_can( 'manage_options' ); },
			'callback'            => function ( WP_REST_Request $req ) {
				$p    = $req->get_json_params();
				$data = isset( $p['data'] ) ? $p['data'] : null;
				if ( ! is_array( $data ) || empty( $data['assets'] ) || empty( $data['order'] ) ) {
					return new WP_Error( 'm360cx_bad', 'Erwartet {data:{order,assets}}', array( 'status' => 400 ) );
				}
				return m360cx_store( $data, ! ( isset( $p['notify'] ) && false === $p['notify'] ) );
			},
		),
	) );
	register_rest_route( 'm360/v1', '/commodix/telegram-test', array(
		'methods'             => 'POST',
		'permission_callback' => function () { return current_user_can( 'manage_options' ); },
		'callback'            => function () {
			$data = json_decode( get_option( 'm360cx_data', '' ), true );
			if ( empty( $data['assets'] ) ) { return new WP_Error( 'm360cx_empty', 'Noch keine Daten', array( 'status' => 404 ) ); }
			$items = array();
			foreach ( $data['order'] as $t ) {
				$sig = isset( $data['assets'][ $t ]['signal'] ) ? $data['assets'][ $t ]['signal'] : '';
				if ( strpos( $sig, 'KAUF' ) === 0 || strpos( $sig, 'VERK' ) === 0 ) { $items[] = $t; }
			}
			if ( ! $items ) { return array( 'ok' => true, 'telegram' => 'keine aktiven Kauf-/Verkaufssignale' ); }
			$res = m360cx_tg_send( m360cx_tg_message( $data, $items, array(), 'Aktive CommodiX-Signale' ) );
			return is_wp_error( $res ) ? $res : array( 'ok' => true, 'telegram' => 'gesendet: ' . implode( ', ', $items ) );
		},
	) );
	// Freie CommodiX-Meldung (z. B. Vorbörse 10 Uhr): {html, key}. key verhindert doppelten Versand.
	register_rest_route( 'm360/v1', '/commodix/telegram-post', array(
		'methods'             => 'POST',
		'permission_callback' => function () { return current_user_can( 'manage_options' ); },
		'callback'            => function ( WP_REST_Request $req ) {
			$p    = $req->get_json_params();
			$key  = isset( $p['key'] ) ? sanitize_key( $p['key'] ) : '';
			$html = isset( $p['html'] ) ? (string) $p['html'] : '';
			$html = wp_kses( $html, array( 'b' => array(), 'strong' => array(), 'i' => array(), 'em' => array(), 'u' => array(), 's' => array(), 'code' => array(), 'pre' => array(), 'a' => array( 'href' => array() ) ) );
			if ( '' === trim( wp_strip_all_tags( $html ) ) || mb_strlen( $html ) > 4000 ) {
				return new WP_Error( 'm360cx_bad', 'html fehlt oder länger als 4000 Zeichen', array( 'status' => 400 ) );
			}
			if ( '' === $key ) { return new WP_Error( 'm360cx_bad', 'key fehlt', array( 'status' => 400 ) ); }
			$keys = get_option( 'm360cx_tg_keys', array() );
			if ( ! is_array( $keys ) ) { $keys = array(); }
			if ( in_array( $key, $keys, true ) ) { return array( 'ok' => true, 'telegram' => 'bereits gesendet: ' . $key ); }
			$res = m360cx_tg_send( $html );
			if ( is_wp_error( $res ) ) { return array( 'ok' => false, 'telegram' => 'Fehler: ' . $res->get_error_message() ); }
			array_unshift( $keys, $key );
			update_option( 'm360cx_tg_keys', array_slice( $keys, 0, 60 ), false );
			return array( 'ok' => true, 'telegram' => 'gesendet: ' . $key );
		},
	) );
	register_rest_route( 'm360/v1', '/commodix/summary', array(
		'methods'             => 'GET',
		'permission_callback' => '__return_true',
		'callback'            => function () {
			$r = new WP_REST_Response( array_merge( (array) get_option( 'm360cx_summary', array() ), array( 'api' => 2 ) ) );
			$r->header( 'Cache-Control', 'no-cache, must-revalidate, max-age=0' );
			return $r;
		},
	) );
} );

// Startseiten-Kachel im Bereich „Meine Finanzwerkzeuge“ (#tools .tools-grid), vor „Investment Depot“
add_action( 'wp_footer', function () {
	if ( ! is_front_page() ) { return; }
	$s = get_option( 'm360cx_summary', array() );
	?>
<script>
(function(){
  var S=<?php echo wp_json_encode( $s ); ?>;
  var grid=document.querySelector('#tools .tools-grid')||document.querySelector('.tools-grid');
  if(!grid||document.getElementById('m360cx-card'))return;
  var href='<?php echo esc_js( home_url( '/commodix/' ) ); ?>';
  var a=document.createElement('a');a.className='tools-card';a.id='m360cx-card';a.href=href;
  a.innerHTML='<div class="tools-icon t-amber" aria-hidden="true">\u{1F4C8}</div><div class="tools-title">CommodiX Rohstoff-Signale</div><p class="tools-desc" id="m360cx-desc">Tägliche Kauf- und Verkaufssignale für Silber, Gold, Minen, Kupfer, Uran, Öl, Erdgas und Seltene Erden – mit Optionsumsetzung.</p><span class="tools-cta">Zu den Signalen</span>';
  var ref=[].slice.call(grid.querySelectorAll('a.tools-card')).filter(function(c){return /Investment Depot/.test(c.textContent)})[0];
  if(ref)grid.insertBefore(a,ref);else grid.appendChild(a);
  function fill(s){
    if(!s||!s.assets||!s.assets.length)return;
    var d=s.asof?new Date(s.asof+'T12:00:00').toLocaleDateString('de-DE'):'';
    var slv=s.assets.filter(function(x){return x.t==='SLV'})[0];
    var w=function(x){return x.sig.split(' ')[0].charAt(0)+x.sig.split(' ')[0].slice(1).toLowerCase()};
    var txt=(slv?'Silber (SLV): '+w(slv)+' ('+(slv.score>0?'+':'')+Math.round(slv.score)+' %) · ':'')+
      s.counts.buy+'× Kaufen · '+s.counts.sell+'× Verkaufen · '+s.counts.neutral+'× Neutral bei '+s.assets.length+' Werten'+(d?' · Stand '+d:'')+(s.status&&!s.status.final&&s.status.time?' '+s.status.time+' Uhr (Zwischenstand)':'');
    var el=document.getElementById('m360cx-desc');if(el)el.textContent=txt;
  }
  fill(S);
  fetch('<?php echo esc_js( rest_url( 'm360/v1/commodix/summary' ) ); ?>',{cache:'no-store'}).then(function(r){return r.ok?r.json():null}).then(fill).catch(function(){});
})();
</script>
	<?php
}, 30 );

/* =========================================================================
 * Vorbörsen-Meldung 10:00 Uhr (Europe/Berlin) – läuft komplett in WordPress, ohne Chrome.
 * Kurse: Yahoo Finance Chart-API (wie Markt-Monitor/Bodensee-Tracker), Signale: m360cx_data.
 * Auslöser: GET /wp-json/m360/v1/commodix/premarket-tick (z. B. geplante Aufgabe per Abruf)
 *           oder erster Seitenaufruf ab 10:00 (init-Sweep). Pro Handelstag genau einmal.
 * Test:     GET /wp-json/m360/v1/commodix/premarket-preview (Admin) -> Text ohne Versand.
 * ========================================================================= */
if ( ! function_exists( 'm360cx_pm_now' ) ) {
	function m360cx_pm_now() { return new DateTime( 'now', new DateTimeZone( 'Europe/Berlin' ) ); }

	function m360cx_pm_us_holidays() {
		return array( '2026-11-26', '2026-12-25', '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26',
			'2027-05-31', '2027-06-18', '2027-07-05', '2027-09-06', '2027-11-25', '2027-12-24' );
	}

	/** Darf heute gesendet werden? (Mo–Fr, 10:00–15:29 Berlin, kein US-Feiertag, noch nicht gesendet) */
	function m360cx_pm_due() {
		$n = m360cx_pm_now();
		$d = $n->format( 'Y-m-d' );
		if ( (int) $n->format( 'N' ) > 5 ) { return 'Wochenende'; }
		if ( in_array( $d, m360cx_pm_us_holidays(), true ) ) { return 'US-Feiertag'; }
		$hm = (int) $n->format( 'Hi' );
		if ( $hm < 1000 ) { return 'vor 10:00'; }
		if ( $hm >= 1530 ) { return 'nach US-Eröffnung'; }
		if ( get_option( 'm360cx_pm_sent', '' ) === $d ) { return 'bereits gesendet'; }
		return true;
	}

	function m360cx_pm_fetch( $syms ) {
		$ua   = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';
		$reqs = array();
		foreach ( $syms as $s ) {
			$reqs[ $s ] = array(
				'url'     => 'https://query1.finance.yahoo.com/v8/finance/chart/' . rawurlencode( $s ) . '?range=1d&interval=5m&includePrePost=true',
				'headers' => array( 'User-Agent' => $ua, 'Accept' => 'application/json' ),
			);
		}
		$out  = array();
		$resp = array();
		$cls  = class_exists( '\WpOrg\Requests\Requests' ) ? '\WpOrg\Requests\Requests' : ( class_exists( 'Requests' ) ? 'Requests' : '' );
		if ( $cls ) {
			try { $resp = $cls::request_multiple( $reqs, array( 'timeout' => 10 ) ); } catch ( \Throwable $e ) { $resp = array(); }
		}
		foreach ( $syms as $s ) {
			$body = '';
			if ( isset( $resp[ $s ] ) && is_object( $resp[ $s ] ) && ! empty( $resp[ $s ]->success ) ) { $body = $resp[ $s ]->body; }
			if ( '' === $body ) {
				$r = wp_remote_get( str_replace( 'query1', 'query2', $reqs[ $s ]['url'] ), array( 'timeout' => 8, 'headers' => $reqs[ $s ]['headers'] ) );
				if ( ! is_wp_error( $r ) && 200 === wp_remote_retrieve_response_code( $r ) ) { $body = wp_remote_retrieve_body( $r ); }
			}
			$j = json_decode( (string) $body, true );
			$m = isset( $j['chart']['result'][0] ) ? $j['chart']['result'][0] : null;
			if ( ! $m ) { $out[ $s ] = null; continue; }
			$meta = $m['meta'];
			$prev = isset( $meta['chartPreviousClose'] ) ? (float) $meta['chartPreviousClose'] : ( isset( $meta['previousClose'] ) ? (float) $meta['previousClose'] : 0 );
			$last = isset( $meta['regularMarketPrice'] ) ? (float) $meta['regularMarketPrice'] : 0;
			$pre  = null;
			$ps   = isset( $meta['currentTradingPeriod']['pre']['start'] ) ? (int) $meta['currentTradingPeriod']['pre']['start'] : 0;
			$pe   = isset( $meta['currentTradingPeriod']['pre']['end'] ) ? (int) $meta['currentTradingPeriod']['pre']['end'] : 0;
			$ts   = isset( $m['timestamp'] ) ? $m['timestamp'] : array();
			$cl   = isset( $m['indicators']['quote'][0]['close'] ) ? $m['indicators']['quote'][0]['close'] : array();
			foreach ( $ts as $i => $t ) {
				if ( $ps && $t >= $ps && $t < $pe && isset( $cl[ $i ] ) && null !== $cl[ $i ] ) { $pre = (float) $cl[ $i ]; }
			}
			$out[ $s ] = array( 'prev' => $prev, 'last' => $last, 'pre' => $pre );
		}
		return $out;
	}

	function m360cx_pm_message( $force = false ) {
		$data = json_decode( get_option( 'm360cx_data', '' ), true );
		if ( empty( $data['assets'] ) ) { return new WP_Error( 'm360cx_empty', 'Keine CommodiX-Daten' ); }
		$e   = function_exists( 'm360tg_esc' ) ? 'm360tg_esc' : 'esc_html';
		$num = function ( $v, $d = 2 ) { return number_format( (float) $v, $d, ',', '.' ); };
		$pct = function ( $v ) use ( $num ) { return ( $v > 0 ? '+' : ( $v < 0 ? '−' : '±' ) ) . $num( abs( $v ), 1 ) . ' %'; };
		$fut = array( 'GC=F' => 'Gold', 'SI=F' => 'Silber', 'HG=F' => 'Kupfer', 'CL=F' => 'WTI', 'NG=F' => 'Erdgas' );
		$q   = m360cx_pm_fetch( array_merge( $data['order'], array_keys( $fut ) ) );
		$n   = m360cx_pm_now();
		$wd  = array( 1 => 'Mo', 2 => 'Di', 3 => 'Mi', 4 => 'Do', 5 => 'Fr', 6 => 'Sa', 7 => 'So' );
		$h   = '🌅 <b>CommodiX Vorbörse</b> · ' . $wd[ (int) $n->format( 'N' ) ] . ' ' . $n->format( 'd.m.Y' ) . ' · ' . $n->format( 'H:i' ) . " Uhr\n";
		// Übernacht (Futures seit Vortagesschluss)
		$ov = array();
		foreach ( $fut as $s => $lbl ) {
			if ( ! empty( $q[ $s ]['prev'] ) && ! empty( $q[ $s ]['last'] ) ) { $ov[] = $lbl . ' ' . $pct( ( $q[ $s ]['last'] / $q[ $s ]['prev'] - 1 ) * 100 ); }
		}
		if ( function_exists( 'rest_do_request' ) ) {
			$r = rest_do_request( new WP_REST_Request( 'GET', '/m360/v1/sge' ) );
			$sg = $r && ! $r->is_error() ? $r->get_data() : null;
			if ( is_array( $sg ) && ! empty( $sg['silver'] ) && is_array( $sg['silver'] ) ) {
				$best = null;
				foreach ( $sg['silver'] as $series ) {
					if ( is_array( $series ) && count( $series ) >= 2 ) { $l = end( $series ); if ( ! $best || $l[0] > $best[1][0] ) { $best = array( $series[ count( $series ) - 2 ], $l ); } }
				}
				if ( $best && $best[0][1] > 0 ) { $ov[] = 'SGE-Silber ' . $pct( ( $best[1][1] / $best[0][1] - 1 ) * 100 ); }
			}
		}
		$h .= $ov ? 'Übernacht: ' . implode( ' · ', $ov ) . "\n" : '';
		// Werte
		$grp = ''; $watch = array(); $traded = 0;
		foreach ( $data['order'] as $t ) {
			if ( empty( $data['assets'][ $t ] ) ) { continue; }
			$a   = $data['assets'][ $t ];
			$sig = (string) $a['signal'];
			if ( $a['group'] !== $grp ) { $grp = $a['group']; $h .= "\n<b>" . $e( $grp ) . "</b>\n"; }
			$ico = strpos( $sig, 'KAUF' ) === 0 ? '🟢' : ( strpos( $sig, 'VERK' ) === 0 ? '🔴' : '⚪' );
			$sk  = strpos( $sig, 'KAUF' ) === 0 ? 'Kaufen' : ( strpos( $sig, 'VERK' ) === 0 ? 'Verkaufen' : 'Neutral' );
			$prev = ! empty( $q[ $t ]['prev'] ) ? $q[ $t ]['prev'] : (float) $a['levels']['last'];
			$pre  = isset( $q[ $t ]['pre'] ) ? $q[ $t ]['pre'] : null;
			$line = $ico . ' <b>' . $e( $t ) . '</b> ';
			$flags = array();
			if ( null === $pre ) {
				$line .= '<i>noch kein Vorbörsenhandel</i> (Schluss ' . $num( $prev ) . ' $)';
			} else {
				$traded++;
				$ch = ( $pre / $prev - 1 ) * 100;
				$line .= $num( $pre ) . ' $ (' . $pct( $ch ) . ')';
				$atr = (float) $a['levels']['atr14'];
				if ( $atr > 0 && abs( $pre - $prev ) > $atr ) { $flags[] = '⚡ Gap'; $watch[] = $t . ': Kurslücke ' . $pct( $ch ); }
				$ot = isset( $a['backtest']['modes']['both']['open_trade'] ) ? $a['backtest']['modes']['both']['open_trade'] : null;
				if ( $ot && ! empty( $ot['stop'] ) && $atr > 0 ) {
					$st = (float) $ot['stop'];
					if ( 'Long' === $ot['dir'] ) {
						if ( $pre < $st ) { $flags[] = '⛔ unter Stop ' . $num( $st ); $watch[] = $t . ': Long-Stop ' . $num( $st ) . ' vorbörslich unterschritten'; }
						elseif ( $pre - $st < 0.5 * $atr ) { $flags[] = '⚠ Stop ' . $num( $st ) . ' nah'; $watch[] = $t . ': Long-Stop ' . $num( $st ) . ' in Reichweite'; }
					} else {
						if ( $pre > $st ) { $flags[] = '⛔ über Stop ' . $num( $st ); $watch[] = $t . ': Short-Stop ' . $num( $st ) . ' vorbörslich überschritten'; }
						elseif ( $st - $pre < 0.5 * $atr ) { $flags[] = '⚠ Stop ' . $num( $st ) . ' nah'; $watch[] = $t . ': Short-Stop ' . $num( $st ) . ' in Reichweite'; }
					}
				}
				$pl = isset( $a['backtest']['modes']['both']['option_plan'] ) ? $a['backtest']['modes']['both']['option_plan'] : array();
				if ( ! empty( $pl['dir'] ) && abs( $ch ) >= 0.3 ) {
					$dearer = ( 'Put' === $pl['type'] ) ? ( $ch < 0 ) : ( $ch > 0 );
					$flags[] = $pl['type'] . ' ' . ( $dearer ? 'teurer' : 'günstiger' );
				}
			}
			$line .= ' · ' . $sk . ( $flags ? ' · ' . implode( ' · ', $flags ) : '' );
			$h .= $line . "\n";
		}
		// Heute im Blick
		$today = $n->format( 'Y-m-d' );
		if ( function_exists( 'm360bt_events' ) ) {
			foreach ( m360bt_events() as $ev ) {
				if ( $ev['d'] === $today && in_array( $ev['k'], array( 'zb' ), true ) ) { $watch[] = html_entity_decode( wp_strip_all_tags( $ev['t'] ), ENT_QUOTES, 'UTF-8' ); }
			}
		}
		if ( ! $traded ) { $watch[] = 'Vorbörse noch ohne Umsätze – Bewegung zeigen vorerst die Futures.'; }
		if ( $watch ) {
			$h .= "\n<b>Heute im Blick</b>\n";
			foreach ( array_slice( array_unique( $watch ), 0, 4 ) as $w ) { $h .= '• ' . $e( $w ) . "\n"; }
		}
		$asof = ! empty( $data['assets'][ $data['order'][0] ]['asof'] ) ? date_i18n( 'd.m.Y', strtotime( $data['assets'][ $data['order'][0] ]['asof'] . ' 12:00:00' ) ) : '';
		$h .= "\n<i>Signale Stand US-Schluss " . $asof . '. Neue Signale entstehen nur zum Tagesschluss. Vorbörsliche Kurse sind dünn gehandelt. Regelbasiert, keine Anlageberatung.</i>';
		return $h;
	}

	function m360cx_pm_run( $source ) {
		$due = m360cx_pm_due();
		if ( true !== $due ) { return $due; }
		// Atomare Tagessperre (add_option schlägt fehl, wenn schon vorhanden) gegen parallele Läufe
		$claim = 'm360cx_pm_claim_' . m360cx_pm_now()->format( 'Ymd' );
		if ( ! add_option( $claim, time(), '', 'no' ) ) {
			if ( time() - (int) get_option( $claim, 0 ) < 5 * MINUTE_IN_SECONDS ) { return 'läuft bereits'; }
			update_option( $claim, time(), false ); // hängengebliebener Lauf -> neu versuchen
		}
		if ( function_exists( 'm360cx_sync' ) ) { m360cx_sync( 'vor Vorbörse' ); }
		$html = m360cx_pm_message();
		if ( is_wp_error( $html ) ) { delete_option( $claim ); return 'Fehler: ' . $html->get_error_message(); }
		$res = m360cx_tg_send( $html );
		if ( is_wp_error( $res ) ) { delete_option( $claim ); return 'Fehler: ' . $res->get_error_message(); }
		$old = get_option( 'm360cx_pm_lastclaim', '' );
		if ( $old && $old !== $claim ) { delete_option( $old ); }
		update_option( 'm360cx_pm_lastclaim', $claim, false );
		update_option( 'm360cx_pm_sent', m360cx_pm_now()->format( 'Y-m-d' ), false );
		if ( function_exists( 'm360tg_log' ) ) { m360tg_log( 'CommodiX Vorbörse gesendet (' . $source . ')' ); }
		return 'gesendet';
	}
}

add_action( 'rest_api_init', function () {
	register_rest_route( 'm360/v1', '/commodix/premarket-tick', array(
		'methods'             => 'GET',
		'permission_callback' => '__return_true',
		'callback'            => function () {
			$r = new WP_REST_Response( array( 'status' => m360cx_pm_run( 'tick' ), 'berlin' => m360cx_pm_now()->format( 'Y-m-d H:i' ) ) );
			$r->header( 'Cache-Control', 'no-store' );
			return $r;
		},
	) );
	register_rest_route( 'm360/v1', '/commodix/premarket-preview', array(
		'methods'             => 'GET',
		'permission_callback' => function () { return current_user_can( 'manage_options' ); },
		'callback'            => function () {
			$h = m360cx_pm_message( true );
			return is_wp_error( $h ) ? $h : array( 'html' => $h, 'due' => m360cx_pm_due(), 'chars' => mb_strlen( $h ) );
		},
	) );
} );

// Rückfallebene ohne externen Auslöser: erster Seitenaufruf ab 10:00 (Berlin) verschickt die Meldung.
add_action( 'init', function () {
	if ( wp_doing_cron() || get_transient( 'm360cx_pm_sweep' ) ) { return; }
	set_transient( 'm360cx_pm_sweep', 1, MINUTE_IN_SECONDS );
	if ( true !== m360cx_pm_due() ) { return; }
	add_action( 'shutdown', function () {
		if ( function_exists( 'fastcgi_finish_request' ) ) { @fastcgi_finish_request(); }
		m360cx_pm_run( 'Seitenaufruf' );
	} );
} );

/* =========================================================================
 * Abend-Abgleich ohne Chrome: Die Cloud-Aufgabe legt commodix-data.json im GitHub-Repository
 * mgfinanz/commodix-data ab. WordPress holt die Datei selbst, übernimmt nur neuere Stände (asof)
 * und meldet neue Kauf-/Verkaufssignale an Telegram (m360cx_store).
 * Auslöser: GET /wp-json/m360/v1/commodix/sync-tick (Cloud-Aufgabe per Abruf) oder Seitenaufrufe
 *           zwischen 22:00 und 10:30 Uhr (höchstens alle 10 Minuten) sowie vor der Vorbörsen-Meldung.
 * ========================================================================= */
if ( ! function_exists( 'm360cx_sync' ) ) {
	function m360cx_sync( $source = '' ) {
		$repo = 'mgfinanz/commodix-data';
		$file = 'commodix-data.json';
		$ua   = array( 'User-Agent' => 'manuel360finanz-commodix', 'Accept' => 'application/vnd.github.raw+json' );
		$r    = wp_remote_get( 'https://api.github.com/repos/' . $repo . '/contents/' . $file . '?ref=main', array( 'timeout' => 20, 'headers' => $ua ) );
		$body = ( ! is_wp_error( $r ) && 200 === wp_remote_retrieve_response_code( $r ) ) ? wp_remote_retrieve_body( $r ) : '';
		if ( '' === $body ) {
			$r    = wp_remote_get( 'https://raw.githubusercontent.com/' . $repo . '/main/' . $file, array( 'timeout' => 20, 'headers' => array( 'User-Agent' => $ua['User-Agent'] ) ) );
			$body = ( ! is_wp_error( $r ) && 200 === wp_remote_retrieve_response_code( $r ) ) ? wp_remote_retrieve_body( $r ) : '';
		}
		if ( '' === $body ) { return 'Fehler: Datei bei GitHub nicht erreichbar'; }
		$data = json_decode( $body, true );
		if ( ! is_array( $data ) || empty( $data['order'] ) || empty( $data['assets'] ) || count( $data['order'] ) < 12 ) { return 'Fehler: Datei unvollständig'; }
		foreach ( $data['order'] as $t ) {
			$a = isset( $data['assets'][ $t ] ) ? $data['assets'][ $t ] : null;
			if ( ! $a || empty( $a['signal'] ) || empty( $a['asof'] ) || empty( $a['levels'] ) || empty( $a['backtest']['modes']['both'] ) ) { return 'Fehler: ' . $t . ' unvollständig'; }
		}
		$new_asof = (string) $data['assets'][ $data['order'][0] ]['asof'];
		$cur      = get_option( 'm360cx_summary', array() );
		$cur_asof = isset( $cur['asof'] ) ? (string) $cur['asof'] : '';
		$ns = isset( $data['status'] ) ? $data['status'] : array( 'date' => $new_asof, 'time' => '', 'final' => true );
		$cs = isset( $cur['status'] ) ? $cur['status'] : array( 'date' => $cur_asof, 'time' => '', 'final' => true );
		$newer = (string) $ns['date'] > (string) $cs['date']
			|| ( (string) $ns['date'] === (string) $cs['date'] && empty( $cs['final'] )
				&& ( ! empty( $ns['final'] ) || (string) $ns['time'] > (string) $cs['time'] ) );
		if ( ! $newer ) { return 'aktuell (' . $cs['date'] . ( empty( $cs['final'] ) ? ' ' . $cs['time'] . ' vorläufig' : '' ) . ')'; }
		$res = m360cx_store( $data, true );
		if ( function_exists( 'm360tg_log' ) ) { m360tg_log( 'CommodiX Daten ' . $new_asof . ' übernommen (' . $source . '), Telegram: ' . $res['telegram'] ); }
		return 'übernommen ' . $new_asof . ( empty( $ns['final'] ) ? ' ' . $ns['time'] . ' (Zwischenstand)' : ' (Tagesschluss)' ) . ' · Telegram: ' . $res['telegram'];
	}
}

add_action( 'rest_api_init', function () {
	register_rest_route( 'm360/v1', '/commodix/sync-tick', array(
		'methods'             => 'GET',
		'permission_callback' => '__return_true',
		'callback'            => function () {
			if ( get_transient( 'm360cx_sync_lock' ) ) {
				$r = new WP_REST_Response( array( 'status' => 'läuft bereits' ) );
			} else {
				set_transient( 'm360cx_sync_lock', 1, MINUTE_IN_SECONDS );
				$r = new WP_REST_Response( array( 'status' => m360cx_sync( 'tick' ) ) );
				delete_transient( 'm360cx_sync_lock' );
			}
			$r->header( 'Cache-Control', 'no-store' );
			return $r;
		},
	) );
} );

// Rückfallebene: Seitenaufrufe prüfen höchstens alle 10 Minuten auf neue Daten (Zwischenstände und Tagesschluss).
add_action( 'init', function () {
	if ( wp_doing_cron() || get_transient( 'm360cx_sync_sweep' ) ) { return; }
	set_transient( 'm360cx_sync_sweep', 1, 10 * MINUTE_IN_SECONDS );
	add_action( 'shutdown', function () {
		if ( function_exists( 'fastcgi_finish_request' ) ) { @fastcgi_finish_request(); }
		if ( ! get_transient( 'm360cx_sync_lock' ) ) {
			set_transient( 'm360cx_sync_lock', 1, MINUTE_IN_SECONDS );
			m360cx_sync( 'Seitenaufruf' );
			delete_transient( 'm360cx_sync_lock' );
		}
	}, 5 );
} );
