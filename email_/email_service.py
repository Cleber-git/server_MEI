import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email_.config import SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD


def enviar_email(destino, assunto, mensagem):

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = destino
    msg["Subject"] = assunto

    msg.attach(MIMEText(mensagem, "html"))
    
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)

        return True

    except Exception as e:
        print("Erro ao enviar email:", e)
        return False