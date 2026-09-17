import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def main():
    sender = "lvbretteam@gmail.com"
    password = "vgxjpykgvuyrqakj"
    receiver = "admin@6framestudio.com"

    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = receiver
    msg['Subject'] = "Marketing Hub: Email Forwarding Test"

    body = "This is a test email sent to verify that admin@6framestudio.com forwarding to Gmail is working."
    msg.attach(MIMEText(body, 'plain'))

    try:
        print("Connecting to Gmail SMTP server...")
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender, password)
        print("Logged in successfully. Sending email...")
        server.sendmail(sender, receiver, msg.as_string())
        server.quit()
        print("Test email sent successfully!")
    except Exception as e:
        print("Failed to send email:", e)

if __name__ == "__main__":
    main()
