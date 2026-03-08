from main import app
from fastapi import HTTPException
from email_.email_model import EmailRequest
from email_.email_service import enviar_email

# router = APIRouter()

@app.post("/enviar-email")
def enviar_email_api(email: EmailRequest):

    sucesso = enviar_email(
        email.para,
        email.assunto,
        email.mensagem
    )

    if not sucesso:
        raise HTTPException(status_code=500, detail="Erro ao enviar email")

    return {"status": 200, "msg": "email enviado"}
