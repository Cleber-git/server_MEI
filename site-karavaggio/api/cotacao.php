<?php

declare(strict_types=1);

use Karavaggio\Api\ApiException;
use Karavaggio\Api\Cors;
use Karavaggio\Api\EmailService;
use Karavaggio\Api\Environment;
use Karavaggio\Api\QuoteController;
use Karavaggio\Api\QuoteValidator;
use Karavaggio\Api\Response;

require dirname(__DIR__) . '/vendor/autoload.php';
Environment::load(dirname(__DIR__) . '/.env');

try {
    (new Cors())->handle();
    (new QuoteController(new QuoteValidator(), new EmailService()))->handle();
} catch (ApiException $exception) {
    Response::json(['detail' => $exception->getMessage()], $exception->statusCode());
} catch (Throwable $exception) {
    error_log('Erro não tratado na API de cotação: ' . $exception->getMessage());
    Response::json(['detail' => 'Erro interno do servidor.'], 500);
}
