from decimal import Decimal
from django.urls import reverse
from django.core.files.images import ImageFile
from django.contrib.staticfiles.testing import (StaticLiveServerTestCase)
from selenium.webdriver.firefox.webdriver import WebDriver
from main import models


class FrontendTest(StaticLiveServerTestCase):
    # запустит Firefox
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.selenium = WebDriver()
        cls.selenium.implicitly_wait(10)

    # выйдет из Firefox
    @classmethod
    def tearDownClass(cls):
        cls.selenium.quit()
        super().tearDownClass()

    # тест product_page страницы на корректность изоражений
    def test_product_page_switches_images_correctly(self):
        product = models.Product.objects.create(name='The cathedral and the bazaar',
                                                slug='cathedral-bazaar',
                                                price=Decimal('10.00'),)
        # создать продукт с тремя изображениями
        for fname in ['cb1.jpg', 'cb2.jpg', 'cb3.jpg']:
            with open('main/fixtures/cb/%s' %fname, 'rb') as f:
                image = models.ProductImage(product=product,
                                            image=ImageFile(f, name=fname),)
                image.save()
        self.selenium.get("%s%s"% (self.live_server_url, reverse("product", kwargs={"slug": "cathedral-bazaar"},),))
        current_image = self.selenium.find_element_by_css_selector(".current-image > img:nth-child(1)").get_attribute("src")
        self.selenium.find_element_by_css_selector("div.image:nth-child(3) > img:nth-child(1)").click()
        new_image = self.selenium.find_element_by_css_selector(".current-image > img:nth-child(1)").get_attribute("src")
        # Полное изображение изменилось на то, по которому щелкнул тест.
        self.assertNotEqual(current_image, new_image)