<?php

declare(strict_types=1);

use Karavaggio\Api\ApiException;
use Karavaggio\Api\ContactController;
use Karavaggio\Api\ContactValidator;
use Karavaggio\Api\Cors;
use Karavaggio\Api\Environment;
use Karavaggio\Api\Response;
use Karavaggio\Api\SacEmailService;

require dirname(__DIR__) . '/vendor/autoload.php';
Environment::load(dirname(__DIR__) . '/.env');

try {
    (new Cors())->handle();
    (new ContactController(new ContactValidator(), new SacEmailService()))->handle();
} catch (ApiException $exception) {
    Response::json(['detail' => $exception->getMessage()], $exception->statusCode());
} catch (Throwable $exception) {
    error_log('Erro não tratado na API do SAC: ' . $exception->getMessage());
    Response::json(['detail' => 'Erro interno do servidor.'], 500);
}
